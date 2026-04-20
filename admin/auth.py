# admin/auth.py
#
# Streamlit is bound to localhost for the admin panel, so no auth is
# needed beyond not deploying it publicly. This stub exists so other
# admin modules can import guard helpers if needed later.


def require_local() -> None:
    """Placeholder — admin runs on localhost only."""
    return None
