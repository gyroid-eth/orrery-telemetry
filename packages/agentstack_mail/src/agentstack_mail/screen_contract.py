"""Shared names for the screen relay, its launchers and the dashboard."""

SERVER = "orrery-screen"
VIEW_TOOLS = ("check_permissions", "list_apps", "list_windows", "get_window_state")
CONTROL_TOOLS = (
    "launch_app", "click", "type_text", "press_key", "hotkey", "scroll", "set_value",
    "invoke_menu",
)
TOOLS = VIEW_TOOLS + CONTROL_TOOLS
MODES = ("off", "view", "control")
OPERATOR_PATH = "/api/computer-use"
CHILD_PATH = OPERATOR_PATH + "/child"
EVENT_PATH = CHILD_PATH + "/events"
