import json
import logging
import urllib.request
import uuid
from pathlib import Path

from django.conf import settings
from django.views.debug import get_exception_reporter_class


class DiscordExceptionHandler(logging.Handler):
    """
    Exception log handler that sends errors to Discord via webhook
    and saves Django HTML debug reports for viewing.
    """

    def __init__(self, **kwargs):
        self.error_report_dir = Path(
            getattr(settings, "ERROR_REPORT_DIR", "/var/lib/amc/error-reports")
        )
        self.admin_domain = getattr(
            settings, "SITE_DOMAIN", "https://www.aseanmotorclub.com"
        )
        logging.Handler.__init__(self)

    def emit(self, record):
        try:
            webhook_url = getattr(settings, "DISCORD_ERRORS_WEBHOOK", None)
            if not webhook_url:
                return

            request = getattr(record, "request", None)

            # Build subject
            try:
                if request:
                    internal_ips = getattr(settings, "INTERNAL_IPS", ())
                    internal = (
                        "internal"
                        if request.META.get("REMOTE_ADDR") in internal_ips
                        else "EXTERNAL"
                    )
                    subject = "{} ({} IP): {}".format(
                        record.levelname,
                        internal,
                        record.getMessage(),
                    )
                else:
                    subject = "{}: {}".format(record.levelname, record.getMessage())
            except Exception:
                subject = "{}: {}".format(record.levelname, record.getMessage())

            subject = self._format_subject(subject)

            # Get exception info
            if record.exc_info:
                exc_info = record.exc_info
            else:
                exc_info = (None, record.getMessage(), None)

            # Generate HTML debug report
            report_url = self._save_html_report(request, exc_info)

            # Build Discord embed
            self._send_discord_embed(record, subject, request, report_url)

        except Exception:
            self.handleError(record)

    def _save_html_report(self, request, exc_info):
        """Save Django's HTML debug report to file and return URL."""
        try:
            # Ensure directory exists
            self.error_report_dir.mkdir(parents=True, exist_ok=True)

            # Generate HTML report
            # pyrefly: ignore [not-callable]
            reporter = get_exception_reporter_class(request)(
                request, is_email=False, *exc_info
            )
            html_content = reporter.get_traceback_html()

            # Save to file
            file_id = str(uuid.uuid4())
            file_path = self.error_report_dir / f"{file_id}.html"
            file_path.write_text(html_content, encoding="utf-8")

            return f"{self.admin_domain}/errors/{file_id}.html"

        except Exception:
            return None

    def _send_discord_embed(self, record, subject, request, report_url):
        """Send error notification to Discord via webhook."""
        webhook_url = getattr(settings, "DISCORD_ERRORS_WEBHOOK", None)
        if not webhook_url:
            return

        # Color based on level
        colors = {
            "CRITICAL": 0x8B0000,  # Dark red
            "ERROR": 0xE74C3C,  # Red
            "WARNING": 0xF39C12,  # Orange
        }
        color = colors.get(record.levelname, 0xE74C3C)

        # Build fields
        fields = []

        if request:
            try:
                fields.append(
                    {"name": "🌐 Path", "value": request.path[:100], "inline": True}
                )
                fields.append(
                    {"name": "🔌 Method", "value": request.method, "inline": True}
                )
                ip = request.META.get("REMOTE_ADDR", "Unknown")
                fields.append({"name": "📍 IP", "value": ip, "inline": True})
            except Exception:
                pass

        fields.append(
            {
                "name": "📁 Location",
                "value": f"`{record.pathname}:{record.lineno}`"[:100],
                "inline": False,
            }
        )

        # Build embed
        embed = {
            "title": f"🚨 {subject[:250]}",
            "color": color,
            "fields": fields,
            "footer": {"text": f"Logger: {record.name}"},
        }

        # Add link to full report
        if report_url:
            embed["url"] = report_url
            embed["description"] = f"[📄 View Full Debug Report]({report_url})"

        try:
            data = json.dumps({"embeds": [embed]}).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass  # Don't let webhook failure cause more errors

    def _format_subject(self, subject):
        """Escape CR and LF characters, and limit length."""
        formatted = subject.replace("\n", "\\n").replace("\r", "\\r")
        return formatted[:250]
