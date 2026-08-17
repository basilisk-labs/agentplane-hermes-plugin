if __package__:
    from .agentplane_hermes_plugin import *  # noqa: F401,F403
else:
    # Keep direct source-tree imports usable for local tests and diagnostics.
    from agentplane_hermes_plugin import *  # noqa: F401,F403
