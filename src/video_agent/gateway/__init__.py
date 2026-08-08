"""gateway — LiteLLM single egress, alias resolution and failure policy.

See ``docs/LLD/gateway.md``.

``gateway.md`` §2 declares ``Alias`` as part of this module's public interface, so callers
import it from here. The enum itself is defined in ``config`` because that is the layer that
owns the table it indexes, and the gateway depends on config rather than the other way round;
re-exporting keeps the documented import path true without inverting the dependency.

Everything a caller needs is re-exported here, and everything re-exported is either in
``gateway.md`` §2 or is a collaborator a deployment has to construct. The internals — the
classification table, the backoff arithmetic, the escaping rules — are reachable by their
module path and are deliberately not surfaced as top-level names, so a call site cannot come to
depend on one and turn a policy detail into a contract.
"""

from video_agent.config.aliases import Alias
from video_agent.gateway.breaker import (
    CircuitBreaker,
    CircuitConfig,
    CircuitState,
    InMemoryCircuitStateStore,
    RedisCircuitStateStore,
    ResilientCircuitStateStore,
)
from video_agent.gateway.cache import InMemoryResponseCache, RedisResponseCache
from video_agent.gateway.capabilities import (
    Capability,
    ProxyCapabilityRegistry,
    StaticCapabilityRegistry,
)
from video_agent.gateway.errors import (
    AliasGroupExhaustedError,
    AliasResolutionError,
    ContentPolicyError,
    ContextLengthExceededError,
    GatewayError,
    PaymentRequiredError,
    PromptRegistryError,
    StructuredOutputError,
    UpstreamRequestError,
)
from video_agent.gateway.gateway import Gateway, GatewayDeps, LiteLLMGateway
from video_agent.gateway.models import (
    AliasHealth,
    ArtifactRef,
    CallContext,
    DegradeReason,
    LLMRequest,
    LLMResponse,
    ModelHealth,
    PromptRef,
    Usage,
)
from video_agent.gateway.prompts import CachingPromptRegistry, FilePromptRegistry, PromptTemplate
from video_agent.gateway.retry import RetryPolicy
from video_agent.gateway.transport import HttpxLiteLLMTransport, LLMTransport

__all__ = [
    "Alias",
    "AliasGroupExhaustedError",
    "AliasHealth",
    "AliasResolutionError",
    "ArtifactRef",
    "CachingPromptRegistry",
    "CallContext",
    "Capability",
    "CircuitBreaker",
    "CircuitConfig",
    "CircuitState",
    "ContentPolicyError",
    "ContextLengthExceededError",
    "DegradeReason",
    "FilePromptRegistry",
    "Gateway",
    "GatewayDeps",
    "GatewayError",
    "HttpxLiteLLMTransport",
    "InMemoryCircuitStateStore",
    "InMemoryResponseCache",
    "LLMRequest",
    "LLMResponse",
    "LLMTransport",
    "LiteLLMGateway",
    "ModelHealth",
    "PaymentRequiredError",
    "PromptRef",
    "PromptRegistryError",
    "PromptTemplate",
    "ProxyCapabilityRegistry",
    "RedisCircuitStateStore",
    "RedisResponseCache",
    "ResilientCircuitStateStore",
    "RetryPolicy",
    "StaticCapabilityRegistry",
    "StructuredOutputError",
    "UpstreamRequestError",
    "Usage",
]
