"""Persistent storage for security scan results.

Stores every security analysis result (regex + guard verdicts + original messages)
for training data collection. Supports human labeling workflow and training data export.
"""

from datetime import datetime, timezone

from sqlalchemy import and_, desc, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.observability import get_logger
from gateway.storage.schema import security_scans

logger = get_logger(__name__)


class SecurityScanStore:
    """Persistent storage for security scan results.

    Writes analysis results to the security_scans table for:
    - Training data collection (messages + verdicts)
    - Human labeling workflow
    - Training data export in finetuning format
    """

    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def store_scan(
        self,
        request_id: str,
        client_id: str,
        messages: list[dict],
        regex_threat_level: str,
        regex_match_count: int,
        *,
        model: str | None = None,
        task: str | None = None,
        regex_matches: list[dict] | None = None,
        guard_safe: bool | None = None,
        guard_skipped: bool | None = None,
        guard_category_code: str | None = None,
        guard_category_name: str | None = None,
        guard_confidence: str | None = None,
        guard_inference_ms: float | None = None,
        guard_raw_response: str | None = None,
        guard_error: str | None = None,
    ) -> None:
        """Store a security scan result."""
        # Determine if regex and guard disagree
        regex_flagged = regex_threat_level not in ("none",)
        has_guard = guard_safe is not None and not guard_skipped
        is_disagreement = False
        if has_guard:
            is_disagreement = regex_flagged != (not guard_safe)

        values = {
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc),
            "client_id": client_id,
            "model": model,
            "task": task,
            "messages": messages,
            "regex_threat_level": regex_threat_level,
            "regex_match_count": regex_match_count,
            "regex_matches": regex_matches,
            "guard_safe": guard_safe,
            "guard_skipped": guard_skipped,
            "guard_category_code": guard_category_code,
            "guard_category_name": guard_category_name,
            "guard_confidence": guard_confidence,
            "guard_inference_ms": guard_inference_ms,
            "guard_raw_response": guard_raw_response,
            "guard_error": guard_error,
            "is_disagreement": is_disagreement,
        }

        try:
            async with self._engine.connect() as conn:
                await conn.execute(insert(security_scans).values(**values))
                await conn.commit()
        except Exception as e:
            logger.error(
                "Failed to store security scan",
                error=str(e),
                request_id=request_id,
            )

    async def label_scan(
        self,
        request_id: str,
        label: str,
        *,
        label_category: str | None = None,
        labeled_by: str | None = None,
        label_notes: str | None = None,
    ) -> bool:
        """Apply a human label to a scan result.

        Args:
            request_id: The request to label
            label: "safe" or "unsafe"
            label_category: Why it's unsafe (e.g. "jailbreak", "injection")
            labeled_by: Who labeled it
            label_notes: Free-form notes

        Returns:
            True if the scan was found and labeled
        """
        values = {
            "label": label,
            "label_category": label_category,
            "labeled_by": labeled_by,
            "labeled_at": datetime.now(timezone.utc),
            "label_notes": label_notes,
        }

        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(
                    update(security_scans)
                    .where(security_scans.c.request_id == request_id)
                    .values(**values)
                )
                await conn.commit()
                return result.rowcount > 0
        except Exception as e:
            logger.error("Failed to label scan", error=str(e), request_id=request_id)
            return False

    async def get_scans(
        self,
        limit: int = 50,
        offset: int = 0,
        label_filter: str | None = None,
        disagreements_only: bool = False,
        unlabeled_only: bool = False,
        min_threat_level: str | None = None,
    ) -> list[dict]:
        """Get security scans with filtering.

        Args:
            limit: Max results
            offset: Skip first N results
            label_filter: Filter by label (safe, unsafe)
            disagreements_only: Only regex/guard disagreements
            unlabeled_only: Only scans without a label
            min_threat_level: Minimum regex threat level
        """
        stmt = (
            select(security_scans)
            .order_by(desc(security_scans.c.timestamp))
            .limit(limit)
            .offset(offset)
        )

        conditions = []
        if label_filter:
            conditions.append(security_scans.c.label == label_filter)
        if disagreements_only:
            conditions.append(security_scans.c.is_disagreement == True)  # noqa: E712
        if unlabeled_only:
            conditions.append(security_scans.c.label.is_(None))
        if min_threat_level:
            threat_levels = _threat_levels_gte(min_threat_level)
            if threat_levels:
                conditions.append(security_scans.c.regex_threat_level.in_(threat_levels))

        if conditions:
            stmt = stmt.where(and_(*conditions))

        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            return [dict(row._mapping) for row in result.fetchall()]

    async def get_scan_by_id(self, request_id: str) -> dict | None:
        """Get a single scan by request_id."""
        stmt = select(security_scans).where(security_scans.c.request_id == request_id)
        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            row = result.fetchone()
            return dict(row._mapping) if row else None

    async def get_label_stats(self) -> dict:
        """Get labeling progress statistics."""
        async with self._engine.connect() as conn:
            # Total scans
            total = (
                await conn.execute(select(func.count()).select_from(security_scans))
            ).scalar() or 0

            # Labeled counts
            labeled = (
                await conn.execute(
                    select(func.count())
                    .select_from(security_scans)
                    .where(security_scans.c.label.isnot(None))
                )
            ).scalar() or 0

            safe_count = (
                await conn.execute(
                    select(func.count())
                    .select_from(security_scans)
                    .where(security_scans.c.label == "safe")
                )
            ).scalar() or 0

            unsafe_count = (
                await conn.execute(
                    select(func.count())
                    .select_from(security_scans)
                    .where(security_scans.c.label == "unsafe")
                )
            ).scalar() or 0

            # Disagreements
            disagreements = (
                await conn.execute(
                    select(func.count())
                    .select_from(security_scans)
                    .where(security_scans.c.is_disagreement == True)  # noqa: E712
                )
            ).scalar() or 0

            unlabeled_disagreements = (
                await conn.execute(
                    select(func.count())
                    .select_from(security_scans)
                    .where(
                        and_(
                            security_scans.c.is_disagreement == True,  # noqa: E712
                            security_scans.c.label.is_(None),
                        )
                    )
                )
            ).scalar() or 0

            return {
                "total_scans": total,
                "labeled": labeled,
                "unlabeled": total - labeled,
                "safe": safe_count,
                "unsafe": unsafe_count,
                "disagreements": disagreements,
                "unlabeled_disagreements": unlabeled_disagreements,
                "label_progress_pct": round(labeled / total * 100, 1) if total > 0 else 0,
            }

    async def export_training_data(
        self,
        format: str = "llama_guard",
        labeled_only: bool = True,
        limit: int = 10000,
    ) -> list[dict]:
        """Export labeled scans as training data.

        Args:
            format: Output format — "llama_guard" or "raw"
            labeled_only: Only export labeled scans
            limit: Max examples to export

        Returns:
            List of training examples in the requested format
        """
        stmt = select(security_scans).order_by(security_scans.c.timestamp).limit(limit)

        if labeled_only:
            stmt = stmt.where(security_scans.c.label.isnot(None))

        async with self._engine.connect() as conn:
            result = await conn.execute(stmt)
            rows = [dict(row._mapping) for row in result.fetchall()]

        if format == "llama_guard":
            return [_to_llama_guard_format(row) for row in rows]
        else:
            return [_to_raw_format(row) for row in rows]


def _to_llama_guard_format(row: dict) -> dict:
    """Convert a scan row to Llama Guard training format.

    Input: conversation messages
    Output: "safe" or "unsafe\\nS1"
    """
    messages = row.get("messages", [])

    # Build conversation text (Llama Guard format)
    conversation_parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            conversation_parts.append({"role": role, "content": content})

    label = row.get("label", "safe")
    label_category = row.get("label_category")

    if label == "unsafe" and label_category:
        target = f"unsafe\n{label_category}"
    elif label == "unsafe":
        target = "unsafe"
    else:
        target = "safe"

    return {
        "messages": conversation_parts,
        "target": target,
        "metadata": {
            "request_id": row.get("request_id"),
            "regex_threat_level": row.get("regex_threat_level"),
            "guard_safe": row.get("guard_safe"),
            "is_disagreement": row.get("is_disagreement"),
            "source": "gateway_production",
        },
    }


def _to_raw_format(row: dict) -> dict:
    """Convert a scan row to raw training format with all fields."""
    return {
        "request_id": row.get("request_id"),
        "messages": row.get("messages", []),
        "label": row.get("label"),
        "label_category": row.get("label_category"),
        "label_notes": row.get("label_notes"),
        "regex_threat_level": row.get("regex_threat_level"),
        "regex_match_count": row.get("regex_match_count"),
        "regex_matches": row.get("regex_matches"),
        "guard_safe": row.get("guard_safe"),
        "guard_category_code": row.get("guard_category_code"),
        "guard_confidence": row.get("guard_confidence"),
        "is_disagreement": row.get("is_disagreement"),
        "client_id": row.get("client_id"),
        "model": row.get("model"),
        "timestamp": row["timestamp"].isoformat() if row.get("timestamp") else None,
    }


def _threat_levels_gte(min_level: str) -> list[str]:
    """Return all threat levels >= the given level."""
    order = ["none", "low", "medium", "high", "critical"]
    try:
        idx = order.index(min_level.lower())
        return order[idx:]
    except ValueError:
        return []
