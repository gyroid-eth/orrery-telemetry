# Excerpt of upstream agent-mail at the pinned ref (5e48183). The enum is
# fail-closed: an unknown mode raises at startup rather than defaulting, which
# is why "passthrough" has to be allowed here before it can be selected.


def _agent_name_mode(value: str) -> str:
    return _enum(
        value,
        default="coerce",
        allowed=frozenset({"strict", "coerce", "always_auto"}),
        key="AGENT_NAME_ENFORCEMENT_MODE",
    )
