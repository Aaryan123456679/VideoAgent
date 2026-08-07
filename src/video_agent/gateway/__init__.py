"""gateway — LiteLLM single egress and alias resolution. See ``docs/LLD/gateway.md``.

``gateway.md`` §2 declares ``Alias`` as part of this module's public interface, so callers
import it from here. The enum itself is defined in ``config`` because that is the layer that
owns the table it indexes, and the gateway depends on config rather than the other way round;
re-exporting keeps the documented import path true without inverting the dependency.
"""

from video_agent.config.aliases import Alias

__all__ = ["Alias"]
