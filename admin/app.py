# admin/app.py
#
# Local Streamlit entrypoint. Never deploy publicly. The individual pages
# under admin/pages/ are auto-discovered by Streamlit's multipage layout.

import streamlit as st

st.set_page_config(page_title="DHRI Admin", layout="wide")

st.title("DHRI VARC — Admin")
st.markdown(
    "Local-only admin panel. Select a page from the sidebar to view dashboards, "
    "browse questions, trigger ingest, inspect users, analytics, or feedback."
)
