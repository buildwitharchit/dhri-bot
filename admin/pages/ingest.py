# admin/pages/ingest.py

import asyncio

import streamlit as st

from scripts.ingest.pipeline import run_seed_ingest, run_full_ingest

st.title("Ingest")

path = st.text_input("Input JSON path", value="data/dhri_48_pyqs_v4.json")
mode = st.radio("Mode", ["Seed (skip tagger + verifier)", "Full tagger"])
skip_verifier = st.checkbox("Also skip verifier in full mode", value=False)

if st.button("Run ingest"):
    st.write("Running…")
    if mode.startswith("Seed"):
        asyncio.run(run_seed_ingest(path))
    else:
        asyncio.run(run_full_ingest(path, skip_verifier=skip_verifier))
    st.success("Done. Check the logs and the questions page for results.")
