import requests

class SessionTracker:
    def __init__(self, backend_url: str):
        self.backend_url = backend_url
        self.applications = []  # accumulates per-application records

    def add_application(self, candidate: str, company: str, ats: str, stats: dict):
        """Call this after each successful application completes."""
        total = stats.get("total_fields", 0)
        auto_rate = 0
        if total > 0:
            auto_rate = ((stats.get("autofill_fields", 0) + stats.get("llm_fields", 0)) / total) * 100

        self.applications.append({
            "candidate_name":  candidate,
            "company_name":    company,
            "ats_platform":    ats,
            "total_fields":    stats.get("total_fields", 0),
            "autofill_fields": stats.get("autofill_fields", 0),
            "llm_fields":      stats.get("llm_fields", 0),
            "human_fields":    stats.get("human_fields", 0),
            "automation_rate": round(auto_rate, 2),
        })

    def send_bulk_summary(self):
        """Call this once at the end of the full session."""
        if not self.applications:
            return
        
        # Determine the correct base URL. If backend_url already has /api, append to it.
        # Otherwise, add /api. The route is mounted at /api/reports/applications/bulk.
        base = self.backend_url.rstrip("/")
        if not base.endswith("/api"):
            base = f"{base}/api"
            
        url = f"{base}/reports/applications/bulk"
        try:
            resp = requests.post(
                url,
                json=self.applications,
                timeout=10
            )
            resp.raise_for_status()
        except Exception as e:
            import logging
            logging.warning(f"Failed to send bulk application summary to {url}: {e}")
            print(f"\n[Warning] Failed to send tracking data to {url}: {e}")
            if 'resp' in locals() and hasattr(resp, 'text'):
                print(f"Response details: {resp.text}")
            pass
            
        self.applications.clear()
