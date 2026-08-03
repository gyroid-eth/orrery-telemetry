# Excerpt of upstream agent-mail at the pinned ref (5e48183), reduced to the
# two decisions the naming patch touches and kept at its original indentation.
# It is not runnable; it exists so the patch anchors are tested against text
# shaped like the real file rather than a convenient approximation.


async def _generate_unique_agent_name(project, settings, name_hint=None):
    mode = getattr(settings, "agent_name_enforcement_mode", "coerce").lower()
    if name_hint:
        _is_reserved = _looks_like_program_name(name_hint) or _looks_like_model_name(name_hint)
        if mode == "always_auto":
            pass  # skip all caller-supplied names, fall through to auto-gen
        elif validate_explicit_agent_id(name_hint) and not _is_reserved:
            # Caller supplied a valid explicit identity (e.g. "alpha-one",
            # "cc-0", "worker_42").  Honor it in strict/coerce modes (#140).
            if not await available(name_hint):
                if mode == "strict":
                    raise ValueError(f"Agent identity '{name_hint}' is already in use.")
                # coerce: fall through to auto-gen
            else:
                return name_hint
        else:
            # Not a valid explicit ID — try legacy adjective+noun path
            sanitized = sanitize_agent_name(name_hint)
            if sanitized:
                if validate_agent_name_format(sanitized):
                    if not await available(sanitized):
                        if mode == "strict":
                            raise ValueError(f"Agent name '{sanitized}' is already in use.")
                    else:
                        return sanitized
                else:
                    if mode == "strict":
                        raise ValueError(
                            f"Invalid agent name format: '{sanitized}'. "
                            f"Use an explicit identity (e.g., 'alpha-one', 'cc-0') "
                            f"or omit the name to auto-generate an adjective+noun name."
                        )


async def _get_or_create_agent(project, name, program, model, task_description, settings):
    mode = getattr(settings, "agent_name_enforcement_mode", "coerce").lower()
    explicit_name_used = False

    if mode == "always_auto" and not window_uuid:
        desired_name = await _generate_unique_agent_name(project, settings, None)
    elif name is not None and mode != "always_auto":
        # Priority 1: Explicit name/identity provided
        _is_reserved = _looks_like_program_name(name) or _looks_like_model_name(name)
        if validate_explicit_agent_id(name) and not _is_reserved:
            desired_name = name
            explicit_name_used = True
        else:
            # Legacy adjective+noun path
            sanitized = sanitize_agent_name(name)
            if not sanitized:
                if mode == "strict":
                    raise ValueError("Agent name must contain alphanumeric characters.")
                desired_name = await _generate_unique_agent_name(project, settings, None)
            elif validate_agent_name_format(sanitized):
                desired_name = sanitized
                explicit_name_used = True
            else:
                if mode == "strict":
                    raise ToolExecutionError("INVALID_AGENT_NAME", "...")
                desired_name = await _generate_unique_agent_name(project, settings, None)
