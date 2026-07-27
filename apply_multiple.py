import subprocess
import time
import sys

# -------------------------------------------------------------
# 1. PASTE YOUR 10 JOB LINKS HERE
# -------------------------------------------------------------
job_links = [
    "https://ashbyhq.com/careers?ashby_jid=c84cf62-5348-4bd3-ad8c-22a77467162d",
    "https://ashbyhq.com/careers?ashby_jid=replace_with_another_id_1",
    "https://ashbyhq.com/careers?ashby_jid=replace_with_another_id_2",
    # Add the rest of your links here...
]

def main():
    print(f"Starting auto-apply for {len(job_links)} jobs...\n")
    
    for i, url in enumerate(job_links, 1):
        if not url or "replace_with" in url:
            continue
            
        print(f"==================================================")
        print(f"[{i}/{len(job_links)}] Applying to: {url}")
        print(f"==================================================")
        
        # Run the CLI for this specific URL in autonomous mode
        try:
            import os
            env = os.environ.copy()
            env["PYTHONPATH"] = "src"
            subprocess.run([
                sys.executable, "-m", "jobcli.cli.entry", 
                "apply", url, 
                "--mode", "auto"  # This ensures it doesn't pause waiting for you!
            ], env=env, check=False)
        except KeyboardInterrupt:
            print("\nStopped by user.")
            break
            
        # Small delay between jobs just to be safe
        time.sleep(2)

    print("\nFinished processing all jobs!")

if __name__ == "__main__":
    main()
