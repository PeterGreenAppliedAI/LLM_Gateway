"""Model discovery, including OpenAI-compatible endpoints, and endpoint credentials (D-036)."""

import httpx
import pytest

from gateway.catalog.discovery import ModelDiscoveryService
from gateway.catalog.models import ModelCatalog
from gateway.config import EndpointConfig, GatewayConfig, ProviderConfig
from gateway.dispatch.registry import ProviderRegistry
from gateway.models.common import ProviderType


def _endpoint(name: str, type_: ProviderType = ProviderType.OPENAI, **kwargs) -> EndpointConfig:
    return EndpointConfig(name=name, type=type_, url=f"http://{name}:8080", **kwargs)


class FakeServers:
    """Answers /v1/models and /api/tags per host; records requests."""

    def __init__(self, models: dict[str, list[dict] | int]):
        self.models = models
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.models.get(request.url.host)
        if isinstance(answer, int):
            return httpx.Response(answer, json={"error": "nope"})
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": answer})
        return httpx.Response(200, json={"object": "list", "data": answer})

    def host_requests(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


async def _discover(endpoints, servers: FakeServers) -> tuple[ModelCatalog, dict]:
    catalog = ModelCatalog()
    service = ModelDiscoveryService(endpoints, catalog)
    service._client = httpx.AsyncClient(transport=httpx.MockTransport(servers))
    results = await service.discover_all()
    await service._client.aclose()
    return catalog, results


class TestOpenAICompatibleDiscovery:
    @pytest.mark.asyncio
    async def test_openai_type_models_discovered_and_routable(self):
        servers = FakeServers({"lmstudio": [{"id": "qwen2.5-7b-instruct"}, {"id": "nomic-embed"}]})
        catalog, results = await _discover([_endpoint("lmstudio")], servers)
        assert results["lmstudio"] == ["qwen2.5-7b-instruct", "nomic-embed"]
        assert catalog.get_endpoints_for_model("qwen2.5-7b-instruct") == ["lmstudio"]

    @pytest.mark.asyncio
    async def test_credentials_sent(self, monkeypatch):
        monkeypatch.setenv("LOCALAI_KEY", "sk-local")
        servers = FakeServers({"localai": [{"id": "llama-3"}]})
        await _discover(
            [_endpoint("localai", api_key_env="LOCALAI_KEY", headers={"X-Org": "acme"})], servers
        )
        sent = servers.host_requests("localai")[0]
        assert sent.headers["Authorization"] == "Bearer sk-local"
        assert sent.headers["X-Org"] == "acme"

    @pytest.mark.asyncio
    async def test_no_auth_header_without_a_key(self):
        servers = FakeServers({"llamacpp": [{"id": "model.gguf"}]})
        await _discover([_endpoint("llamacpp")], servers)
        assert "Authorization" not in servers.host_requests("llamacpp")[0].headers

    @pytest.mark.asyncio
    async def test_failing_endpoint_does_not_block_others(self):
        servers = FakeServers({"down": 503, "up": [{"id": "phi4"}]})
        catalog, results = await _discover([_endpoint("down"), _endpoint("up")], servers)
        assert results == {"down": [], "up": ["phi4"]}
        assert catalog.get_endpoints_for_model("phi4") == ["up"]

    @pytest.mark.asyncio
    async def test_vllm_still_discovered(self):
        servers = FakeServers({"vllm": [{"id": "meta-llama/Llama-3.1-8B-Instruct"}]})
        catalog, _ = await _discover([_endpoint("vllm", ProviderType.VLLM)], servers)
        assert catalog.get_endpoints_for_model("meta-llama/Llama-3.1-8B-Instruct") == ["vllm"]


class TestMediaModelsStayOutOfTextCatalog:
    @pytest.mark.asyncio
    async def test_media_endpoint_skipped_by_default(self):
        servers = FakeServers({"kokoro": [{"id": "kokoro"}, {"id": "tts-1"}]})
        catalog, results = await _discover([_endpoint("kokoro", capabilities=["tts"])], servers)
        assert results == {}
        assert servers.requests == []  # not even asked
        assert catalog.get_endpoints_for_model("kokoro") == []

    @pytest.mark.asyncio
    async def test_mixed_server_opts_in_and_media_tasks_filtered(self):
        servers = FakeServers(
            {
                "localai": [
                    {"id": "llama-3.1-8b"},
                    {"id": "Systran/faster-whisper-small", "task": "automatic-speech-recognition"},
                    {"id": "kokoro", "task": "text-to-speech"},
                ]
            }
        )
        catalog, results = await _discover(
            [_endpoint("localai", capabilities=["tts", "stt"], serves_text=True)], servers
        )
        assert results["localai"] == ["llama-3.1-8b"]

    def test_text_models_rule(self):
        assert _endpoint("plain").text_models
        assert not _endpoint("voice", capabilities=["stt"]).text_models
        assert _endpoint("mixed", capabilities=["stt"], serves_text=True).text_models
        assert not _endpoint("off", serves_text=False).text_models


class TestEndpointCredentials:
    """Regression: credentials were dropped between config and adapter."""

    @pytest.mark.asyncio
    async def test_endpoint_format_api_key_env(self, monkeypatch):
        monkeypatch.setenv("CLOUD_KEY", "sk-cloud")
        config = GatewayConfig(
            endpoints=[_endpoint("cloud", api_key_env="CLOUD_KEY", headers={"X-Org": "acme"})]
        )
        registry = ProviderRegistry(config)
        await registry.initialize()
        adapter = registry.get("cloud")
        assert adapter._api_key == "sk-cloud"
        assert adapter._custom_headers == {"X-Org": "acme"}
        await registry.close()

    @pytest.mark.asyncio
    async def test_legacy_provider_api_key_and_headers(self, monkeypatch):
        monkeypatch.setenv("CLOUD_KEY", "sk-cloud")
        config = GatewayConfig(
            providers=[
                ProviderConfig(
                    name="cloud",
                    type=ProviderType.OPENAI,
                    base_url="https://api.example.com",
                    api_key="${CLOUD_KEY}",
                    headers={"anthropic-version": "2023-06-01"},
                )
            ]
        )
        registry = ProviderRegistry(config)
        await registry.initialize()
        adapter = registry.get("cloud")
        assert adapter._api_key == "sk-cloud"
        assert adapter._custom_headers == {"anthropic-version": "2023-06-01"}
        await registry.close()

    @pytest.mark.asyncio
    async def test_vllm_sends_api_key(self, monkeypatch):
        monkeypatch.setenv("VLLM_KEY", "token-abc")
        config = GatewayConfig(
            endpoints=[_endpoint("vllm", ProviderType.VLLM, api_key_env="VLLM_KEY")]
        )
        registry = ProviderRegistry(config)
        await registry.initialize()
        client = await registry.get("vllm")._get_client()
        assert client.headers["Authorization"] == "Bearer token-abc"
        await registry.close()
