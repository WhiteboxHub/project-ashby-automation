import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console

console = Console()

def send_daily_report(sync_server_url: str, mock: bool = False):
    """Fetch today's stats from the backend and send an email using CLI's .env credentials."""

    # Load CLI project's .env file dynamically.
    # Check multiple candidate paths in priority order:
    #   1. Current working directory (where the user ran wboxcli from)
    #   2. 4 levels up from this file (installed layout: ~/.jobcli/src/src/jobcli/analytics/)
    #   3. 3 levels up from this file (editable/source layout: src/jobcli/analytics/)
    #   4. plain load_dotenv() as last resort (picks up shell environment)
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent.parent.parent / ".env",
        Path(__file__).resolve().parent.parent.parent.parent.parent / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",
    ]
    loaded = False
    for env_path in candidates:
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=True)
            loaded = True
            break
    if not loaded:
        load_dotenv(override=True)

    # 1. Fetch data from Backend
    base = sync_server_url.rstrip("/")
    if not base.endswith("/api"):
        base = f"{base}/api"
    url = f"{base}/reports/applications/today"
    if mock:
        url += "?mock=true"
    
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        records = resp.json()
    except Exception as e:
        console.print(f"[red]Failed to fetch today's stats from {url}: {e}[/red]")
        return
        
    today = date.today()
    if not records:
        console.print(f"[yellow]No applications submitted on {today}. Skipping email.[/yellow]")
        return

    # 2. Compute totals
    total_apps     = len(records)
    total_fields   = sum(r.get("total_fields", 0) for r in records)
    total_autofill = sum(r.get("autofill_fields", 0) for r in records)
    total_llm      = sum(r.get("llm_fields", 0) for r in records)
    total_human    = sum(r.get("human_fields", 0) for r in records)
    overall_auto   = round(((total_autofill + total_llm) / total_fields) * 100, 2) if total_fields else 0

    table_rows = "".join(f"""
        <tr>
            <td>{r.get('candidate_name')}</td><td>{r.get('company_name')}</td>
            <td>{r.get('ats_platform')}</td><td>{r.get('total_fields', 0)}</td>
            <td>{r.get('autofill_fields', 0)}</td><td>{r.get('llm_fields', 0)}</td>
            <td>{r.get('human_fields', 0)}</td><td>{r.get('automation_rate', 0)}%</td>
        </tr>""" for r in records)

    html = f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f9f9f9; color: #333; padding: 20px; margin: 0; }}
            .email-container {{ max-width: 960px; margin: 0 auto; background: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
            .header {{ background: linear-gradient(135deg, #1a73e8 0%, #0d47a1 100%); color: #fff; text-align: center; padding: 25px; border-radius: 8px 8px 0 0; margin: -30px -30px 30px -30px; }}
            .header h1 {{ margin: 0; font-size: 24px; }}
            .header .subtitle {{ margin: 5px 0 0; font-size: 14px; opacity: 0.9; }}
            .summary-grid {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
            .stat-box {{ flex: 1; min-width: 130px; background: #f1f3f4; border-radius: 8px; padding: 16px; text-align: center; }}
            .stat-box .value {{ font-size: 28px; font-weight: bold; color: #1a73e8; }}
            .stat-box .label {{ font-size: 12px; color: #666; margin-top: 4px; }}
            table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
            th {{ background-color: #1a73e8; color: #fff; padding: 10px 12px; text-align: left; }}
            td {{ padding: 9px 12px; border-bottom: 1px solid #e0e0e0; }}
            tr:nth-child(even) td {{ background-color: #f8f9fa; }}
            .footer {{ text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #eee; color: #777; font-size: 13px; }}
        </style>
    </head>
    <body>
        <div class="email-container">
            <div class="header">
                <h1>📊 Daily ATS Application Report</h1>
                <div class="subtitle">{today.strftime('%B %d, %Y')} — Automation Analytics</div>
            </div>

            <div class="summary-grid">
                <div class="stat-box"><div class="value">{total_apps}</div><div class="label">Total Applications</div></div>
                <div class="stat-box"><div class="value">{total_fields}</div><div class="label">Total Fields</div></div>
                <div class="stat-box"><div class="value">{total_autofill}</div><div class="label">Autofill Fields</div></div>
                <div class="stat-box"><div class="value">{total_llm}</div><div class="label">LLM Fields</div></div>
                <div class="stat-box"><div class="value">{total_human}</div><div class="label">Human Fields</div></div>
                <div class="stat-box"><div class="value">{overall_auto}%</div><div class="label">Automation Rate</div></div>
            </div>

            <table>
                <tr>
                    <th>Candidate</th><th>Company</th><th>ATS Platform</th>
                    <th>Total Fields</th><th>Autofill</th><th>LLM</th><th>Human</th><th>Auto %</th>
                </tr>
                {table_rows}
            </table>

            <div class="footer">
                <p>This report was automatically generated by the WBox CLI ATS Automation System.</p>
                <p>Best regards,<br><strong>Innovapath Automation Team</strong></p>
            </div>
        </div>
    </body>
    </html>
    """

    # 3. Read SMTP credentials from CLI .env (strip any accidental whitespace)
    smtp_user  = (os.getenv("SMTP_USER") or "").strip()
    smtp_pass  = (os.getenv("SMTP_PASS") or "").strip()
    admin_email = (os.getenv("ADMIN_EMAIL") or "").strip()

    if not all([smtp_user, smtp_pass, admin_email]):
        console.print("[red]Missing SMTP_USER, SMTP_PASS, or ADMIN_EMAIL in CLI .env[/red]")
        return

    # 4. Send Email
    subject = f"CLI Daily ATS Report – {today.strftime('%Y-%m-%d')}"
    msg = MIMEMultipart("alternative")
    msg['Subject'] = subject
    msg['From'] = smtp_user
    msg['To'] = admin_email
    
    msg.attach(MIMEText("Please view this email in an HTML-compatible client.", 'plain'))
    msg.attach(MIMEText(html, 'html'))

    try:
        # Assumes Gmail based on earlier project hints, adjust if needed
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        console.print(f"[green]Report sent successfully to {admin_email} for {total_apps} applications![/green]")
    except Exception as e:
        console.print(f"[red]Failed to send email: {e}[/red]")
