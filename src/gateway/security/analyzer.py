"""Async security analysis for post-hoc threat detection.

Runs security analysis in the background without adding latency to requests.
Stores results for alerting and pattern analysis.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Callable, Awaitable
from collections import deque

from gateway.security.sanitizer import Sanitizer, SanitizationResult
from gateway.security.injection import InjectionDetector, DetectionResult, ThreatLevel
from gateway.security.guard import GuardResult, LlamaGuardClient, GraniteGuardianClient
from gateway.observability import get_logger

logger = get_logger(__name__)


class AlertSeverity(str, Enum):
    """Alert severity levels."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class SecurityAlert:
    """A security alert generated from analysis."""
    timestamp: str
    request_id: str
    client_id: str
    severity: AlertSeverity
    alert_type: str
    description: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "request_id": self.request_id,
            "client_id": self.client_id,
            "severity": self.severity.value,
            "alert_type": self.alert_type,
            "description": self.description,
            "details": self.details,
        }


@dataclass
class AnalysisRequest:
    """A request queued for security analysis."""
    request_id: str
    client_id: str
    model: str
    messages: list[dict]
    task: Optional[str] = None
    response_content: Optional[str] = None
    source_ip: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class AnalysisResult:
    """Result of security analysis."""
    request_id: str
    sanitization: Optional[SanitizationResult]
    injection_scan: Optional[DetectionResult]
    guard_scan: Optional[GuardResult] = None
    alerts: list[SecurityAlert] = field(default_factory=list)
    analyzed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "analyzed_at": self.analyzed_at,
            "sanitization": self.sanitization.to_dict() if self.sanitization else None,
            "injection_scan": self.injection_scan.to_dict() if self.injection_scan else None,
            "guard_scan": self.guard_scan.to_dict() if self.guard_scan else None,
            "alerts": [a.to_dict() for a in self.alerts],
        }


class AsyncSecurityAnalyzer:
    """Background security analyzer.

    Processes requests asynchronously without blocking the request path.
    Generates alerts for suspicious patterns.

    Usage:
        analyzer = AsyncSecurityAnalyzer()
        await analyzer.start()

        # Queue requests for analysis
        analyzer.queue_request(request_id, client_id, messages)

        # Later, check for alerts
        alerts = analyzer.get_recent_alerts()

        await analyzer.stop()
    """

    def __init__(
        self,
        sanitizer: Optional[Sanitizer] = None,
        detector: Optional[InjectionDetector] = None,
        guard_client: Optional[LlamaGuardClient | GraniteGuardianClient] = None,
        scan_allowlist_ips: Optional[list[str]] = None,
        max_queue_size: int = 1000,
        max_alerts: int = 1000,
        alert_callback: Optional[Callable[[SecurityAlert], Awaitable[None]]] = None,
    ):
        """Initialize the analyzer.

        Args:
            sanitizer: Sanitizer instance (uses default if None)
            detector: InjectionDetector instance (uses default if None)
            guard_client: Optional LlamaGuardClient for shadow analysis
            scan_allowlist_ips: Source IPs to skip scanning (trusted services)
            max_queue_size: Maximum pending analysis requests
            max_alerts: Maximum alerts to retain in memory
            alert_callback: Optional async callback for new alerts
        """
        self.sanitizer = sanitizer or Sanitizer()
        self.detector = detector or InjectionDetector()
        self.guard_client = guard_client
        self._scan_allowlist_ips: set[str] = set(scan_allowlist_ips or [])
        # Detector for embeddings: skip delimiter attacks to avoid false positives
        # from model vocabulary tokens like [SYSTEM], [INST] etc.
        self._embedding_detector = InjectionDetector(check_delimiter_attacks=False)
        self.max_queue_size = max_queue_size
        self.max_alerts = max_alerts
        self.alert_callback = alert_callback

        self._queue: asyncio.Queue[AnalysisRequest] = asyncio.Queue(maxsize=max_queue_size)
        self._alerts: deque[SecurityAlert] = deque(maxlen=max_alerts)
        self._results: deque[AnalysisResult] = deque(maxlen=max_alerts)
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Statistics
        self._stats = {
            "requests_analyzed": 0,
            "alerts_generated": 0,
            "requests_dropped": 0,
            "requests_allowlisted": 0,
            "guard_scans": 0,
            "guard_skipped": 0,
            "guard_unsafe": 0,
        }

    async def start(self) -> None:
        """Start the background analysis task."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._analysis_loop())
        logger.info("Security analyzer started")

    async def stop(self) -> None:
        """Stop the background analysis task."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.guard_client:
            await self.guard_client.close()
        logger.info("Security analyzer stopped", stats=self._stats)

    def queue_request(
        self,
        request_id: str,
        client_id: str,
        model: str,
        messages: list[dict],
        task: Optional[str] = None,
        response_content: Optional[str] = None,
        source_ip: Optional[str] = None,
    ) -> bool:
        """Queue a request for background analysis.

        Args:
            request_id: Unique request identifier
            client_id: Client/API key identifier
            model: Model being used
            messages: Request messages
            task: Task type (chat, completion, embeddings)
            response_content: Optional response content to analyze
            source_ip: Source IP address of the request

        Returns:
            True if queued, False if queue is full or allowlisted
        """
        # Skip scanning for allowlisted IPs (trusted internal services)
        if source_ip and source_ip in self._scan_allowlist_ips:
            self._stats["requests_allowlisted"] += 1
            logger.debug(
                "Security scan skipped (allowlisted IP)",
                request_id=request_id,
                source_ip=source_ip,
                client_id=client_id,
            )
            return False

        request = AnalysisRequest(
            request_id=request_id,
            client_id=client_id,
            model=model,
            messages=messages,
            task=task,
            response_content=response_content,
            source_ip=source_ip,
        )

        try:
            self._queue.put_nowait(request)
            return True
        except asyncio.QueueFull:
            self._stats["requests_dropped"] += 1
            logger.warning(
                "Security analysis queue full, request dropped",
                request_id=request_id,
            )
            return False

    async def _analysis_loop(self) -> None:
        """Main analysis loop running in background."""
        while self._running:
            try:
                # Wait for next request with timeout
                try:
                    request = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Analyze the request
                result = await self._analyze_request(request)
                self._results.append(result)
                self._stats["requests_analyzed"] += 1

                # Process any alerts
                for alert in result.alerts:
                    self._alerts.append(alert)
                    self._stats["alerts_generated"] += 1

                    # Call alert callback if configured
                    if self.alert_callback:
                        try:
                            await self.alert_callback(alert)
                        except Exception as e:
                            logger.error("Alert callback failed", error=str(e))

                    # Log the alert
                    logger.warning(
                        "Security alert",
                        alert_type=alert.alert_type,
                        severity=alert.severity.value,
                        request_id=alert.request_id,
                        client_id=alert.client_id,
                        description=alert.description,
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in security analysis loop", error=str(e))

    async def _analyze_request(self, request: AnalysisRequest) -> AnalysisResult:
        """Analyze a single request.

        Args:
            request: The analysis request

        Returns:
            AnalysisResult with findings
        """
        alerts: list[SecurityAlert] = []

        # Combine all message content for analysis
        combined_content = "\n".join(
            msg.get("content", "")
            for msg in request.messages
            if isinstance(msg.get("content"), str)
        )

        # Run sanitization analysis (check what would be removed)
        sanitization = self.sanitizer.sanitize(combined_content)

        # Check if sanitization found anything
        if sanitization.modified:
            alerts.append(SecurityAlert(
                timestamp=datetime.now(timezone.utc).isoformat(),
                request_id=request.request_id,
                client_id=request.client_id,
                severity=AlertSeverity.WARNING,
                alert_type="unicode_manipulation",
                description=f"Found {sanitization.total_removals} suspicious Unicode characters",
                details=sanitization.to_dict(),
            ))

        # Run injection pattern scan
        # Use embedding-specific detector (no delimiter attack patterns) for embedding
        # tasks to avoid false positives from model vocabulary tokens like [SYSTEM]
        detector = self._embedding_detector if request.task == "embeddings" else self.detector
        injection_scan = detector.scan_messages(request.messages)

        # Generate alerts based on threat level
        if injection_scan.threat_level == ThreatLevel.CRITICAL:
            alerts.append(SecurityAlert(
                timestamp=datetime.now(timezone.utc).isoformat(),
                request_id=request.request_id,
                client_id=request.client_id,
                severity=AlertSeverity.CRITICAL,
                alert_type="injection_critical",
                description=f"Critical injection pattern detected: {injection_scan.matches[0].pattern_name if injection_scan.matches else 'unknown'}",
                details=injection_scan.to_dict(),
            ))
        elif injection_scan.threat_level == ThreatLevel.HIGH:
            alerts.append(SecurityAlert(
                timestamp=datetime.now(timezone.utc).isoformat(),
                request_id=request.request_id,
                client_id=request.client_id,
                severity=AlertSeverity.WARNING,
                alert_type="injection_high",
                description=f"High-threat injection pattern detected: {injection_scan.matches[0].pattern_name if injection_scan.matches else 'unknown'}",
                details=injection_scan.to_dict(),
            ))
        elif injection_scan.threat_level == ThreatLevel.MEDIUM:
            alerts.append(SecurityAlert(
                timestamp=datetime.now(timezone.utc).isoformat(),
                request_id=request.request_id,
                client_id=request.client_id,
                severity=AlertSeverity.INFO,
                alert_type="injection_medium",
                description=f"Medium-threat injection pattern detected: {injection_scan.matches[0].pattern_name if injection_scan.matches else 'unknown'}",
                details=injection_scan.to_dict(),
            ))

        # Run guard model shadow scan (informational only — no alerts)
        guard_scan = None
        if self.guard_client:
            guard_scan = await self.guard_client.classify(request.messages)
            self._stats["guard_scans"] += 1
            if guard_scan.skipped:
                self._stats["guard_skipped"] += 1
            elif not guard_scan.safe:
                self._stats["guard_unsafe"] += 1

            # Log comparison for analysis
            logger.info(
                "Guard model shadow result",
                request_id=request.request_id,
                guard_safe=guard_scan.safe,
                guard_skipped=guard_scan.skipped,
                guard_category=guard_scan.category_code,
                guard_inference_ms=guard_scan.inference_time_ms,
                regex_threat_level=injection_scan.threat_level.value,
                regex_match_count=injection_scan.match_count,
            )

        return AnalysisResult(
            request_id=request.request_id,
            sanitization=sanitization,
            injection_scan=injection_scan,
            guard_scan=guard_scan,
            alerts=alerts,
        )

    def get_recent_alerts(self, limit: int = 100) -> list[SecurityAlert]:
        """Get recent security alerts.

        Args:
            limit: Maximum number of alerts to return

        Returns:
            List of recent alerts (newest first)
        """
        alerts = list(self._alerts)
        alerts.reverse()
        return alerts[:limit]

    def get_recent_results(self, limit: int = 100) -> list[AnalysisResult]:
        """Get recent analysis results.

        Args:
            limit: Maximum number of results to return

        Returns:
            List of recent results (newest first)
        """
        results = list(self._results)
        results.reverse()
        return results[:limit]

    def get_stats(self) -> dict:
        """Get analyzer statistics."""
        return {
            **self._stats,
            "queue_size": self._queue.qsize(),
            "alerts_in_memory": len(self._alerts),
        }

    def clear_alerts(self) -> int:
        """Clear all alerts from memory.

        Returns:
            Number of alerts cleared
        """
        count = len(self._alerts)
        self._alerts.clear()
        return count


# Global analyzer instance (lazy initialization)
_analyzer: Optional[AsyncSecurityAnalyzer] = None


def get_analyzer() -> AsyncSecurityAnalyzer:
    """Get the global security analyzer instance."""
    global _analyzer
    if _analyzer is None:
        _analyzer = AsyncSecurityAnalyzer()
    return _analyzer


async def start_analyzer() -> None:
    """Start the global security analyzer."""
    analyzer = get_analyzer()
    await analyzer.start()


async def stop_analyzer() -> None:
    """Stop the global security analyzer."""
    if _analyzer is not None:
        await _analyzer.stop()
