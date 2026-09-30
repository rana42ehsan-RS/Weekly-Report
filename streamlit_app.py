import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import streamlit as st


BASE_DIR = Path(__file__).resolve().parent
GENERATOR = BASE_DIR / "weekly_aqi_report.py"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

st.set_page_config(page_title="Weekly AQI Report", page_icon="AQI")
st.title("Weekly AQI Report")

with st.form("report_form"):
    uploaded_file = st.file_uploader("District or station CSV", type=["csv"])
    use_custom_week = st.checkbox("Choose a specific reporting week")
    week_start = st.date_input(
        "Week starts",
        value=date.today() - timedelta(days=6),
        disabled=not use_custom_week,
    )
    submitted = st.form_submit_button("Generate report")

if submitted:
    st.session_state.pop("report", None)

    if uploaded_file is None:
        st.error("Choose a CSV file first.")
        st.stop()
    if Path(uploaded_file.name).suffix.lower() != ".csv":
        st.error("Upload a .csv file.")
        st.stop()
    if uploaded_file.size > MAX_UPLOAD_BYTES:
        st.error("The file is larger than the 100 MB limit.")
        st.stop()

    with tempfile.TemporaryDirectory(prefix="weekly-aqi-") as temp_dir:
        job_dir = Path(temp_dir)
        input_path = job_dir / "input.csv"
        input_path.write_bytes(uploaded_file.getvalue())
        command = [
            sys.executable,
            str(GENERATOR),
            "--input",
            str(input_path),
            "--output-dir",
            str(job_dir),
        ]
        if use_custom_week:
            command.extend(["--start", week_start.isoformat()])

        try:
            result = subprocess.run(
                command,
                cwd=BASE_DIR,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
        except subprocess.TimeoutExpired:
            st.error("Report generation took too long. Try a smaller file.")
            st.stop()

        if result.returncode != 0:
            details = (
                result.stderr
                or result.stdout
                or "The report generator stopped unexpectedly."
            )[-2500:]
            st.error(details)
            st.stop()

        pdf_files = sorted(job_dir.glob("Weekly_AQI_Report_*.pdf"))
        excel_files = sorted(job_dir.glob("Weekly_AQI_Stats_*.xlsx"))
        if not pdf_files or not excel_files:
            st.error("The generator did not produce both report files.")
            st.stop()

        st.session_state["report"] = {
            "pdf_name": pdf_files[0].name,
            "pdf": pdf_files[0].read_bytes(),
            "excel_name": excel_files[0].name,
            "excel": excel_files[0].read_bytes(),
        }

report = st.session_state.get("report")
if report:
    st.success("Reports are ready.")
    pdf_column, excel_column = st.columns(2)
    with pdf_column:
        st.download_button(
            "Download PDF",
            data=report["pdf"],
            file_name=report["pdf_name"],
            mime="application/pdf",
            use_container_width=True,
        )
    with excel_column:
        st.download_button(
            "Download Excel",
            data=report["excel"],
            file_name=report["excel_name"],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )