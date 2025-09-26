import streamlit as st

def inject_button_css():
    st.markdown("""
    <style>
    .stButton>button, div[data-testid="baseButton-secondary"] {
      background-color: #3A7BD5 !important;
      color: white !important;
      border: 1px solid #316bb8 !important;
      border-radius: 8px !important;
    }
    .stButton>button:hover, div[data-testid="baseButton-secondary"]:hover {
      background-color: #356fbe !important;
      border-color: #2f63a8 !important;
    }
    </style>
    """, unsafe_allow_html=True)
