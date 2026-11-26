# -*- coding: utf-8 -*-
"""
Pre-Flight AI Consultation Module

Provides an interactive pre-execution phase where the operator can:
  1. Upload screenshots (image files) for AI vision analysis
  2. Chat with the AI about target post types and behaviors
  3. Receive AI explanations of post categories and recommended actions
  4. Get a structured consultation report that informs execution strategy

Runs BEFORE the main attack loop. Results are merged into target_info
for display and used to bias vector selection, pacing, and session strategy.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .gemini_client import GeminiClient, _extract_json_dict


@dataclass(slots=True)
class ScreenshotAnalysis:
    """Result of analyzing one uploaded screenshot."""
    filepath: str
    description: str = ""
    post_types_detected: List[str] = field(default_factory=list)
    suspicious_indicators: List[str] = field(default_factory=list)
    recommended_action: str = ""
    confidence: float = 0.0


@dataclass(slots=True)
class ConsultationReport:
    """Complete pre-execution AI consultation output."""
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    target_username: str = ""
    screenshot_analyses: List[ScreenshotAnalysis] = field(default_factory=list)
    chat_summary: str = ""
    post_types_identified: List[str] = field(default_factory=list)
    risk_assessment: str = "Medium"
    recommended_vectors: List[str] = field(default_factory=list)
    avoid_vectors: List[str] = field(default_factory=list)
    pacing_recommendation: str = "5-15s randomized jitter"
    session_rotation: bool = False
    device_mutation: bool = False
    ai_reasoning: str = ""
    confidence_score: float = 0.7
    raw_ai_response: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "target_username": self.target_username,
            "screenshot_analyses": [
                {
                    "filepath": sa.filepath,
                    "description": sa.description,
                    "post_types_detected": sa.post_types_detected,
                    "suspicious_indicators": sa.suspicious_indicators,
                    "recommended_action": sa.recommended_action,
                    "confidence": sa.confidence,
                }
                for sa in self.screenshot_analyses
            ],
            "chat_summary": self.chat_summary,
            "post_types_identified": self.post_types_identified,
            "risk_assessment": self.risk_assessment,
            "recommended_vectors": self.recommended_vectors,
            "avoid_vectors": self.avoid_vectors,
            "pacing_recommendation": self.pacing_recommendation,
            "session_rotation": self.session_rotation,
            "device_mutation": self.device_mutation,
            "ai_reasoning": self.ai_reasoning,
            "confidence_score": self.confidence_score,
            "raw_ai_response": self.raw_ai_response,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────────────
# Image Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _image_to_data_uri(path: str) -> Optional[str]:
    """Convert an image file to a base64 data URI for vision-capable models."""
    if not os.path.exists(path):
        return None
    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        return None
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# AI Consultation Engine
# ──────────────────────────────────────────────────────────────────────────────

class PreflightConsultation:
    """
    Interactive pre-execution AI consultation.

    The operator can:
      • upload screenshots (local image paths) for vision analysis
      • send free-text messages describing posts / behaviors they've seen
      • ask the AI to explain what post types exist on the target profile
      • receive AI recommendations on vectors, pacing, and session strategy

    Returns a ConsultationReport that is merged into the execution context.
    """

    def __init__(self, gemini_client: GeminiClient):
        self.gemini = gemini_client
        self.conversation_history: List[Dict[str, str]] = []
        self.screenshot_analyses: List[ScreenshotAnalysis] = []

    # ─── Screenshot Analysis ──────────────────────────────────────────────────

    def analyze_screenshot(self, filepath: str) -> ScreenshotAnalysis:
        """Send one screenshot to the AI for vision analysis."""
        data_uri = _image_to_data_uri(filepath)
        if data_uri is None:
            return ScreenshotAnalysis(
                filepath=filepath,
                description="Could not read or parse image file.",
                confidence=0.0,
            )

        prompt = (
            "You are an AI analyst reviewing a screenshot from a TikTok profile page.\n"
            "Analyze the image and reply ONLY with valid JSON containing these keys:\n"
            '{\n'
            '  "description": "1-2 sentence plain-English description of what is visible",\n'
            '  "post_types_detected": ["list of post categories seen, e.g. dance, prank, political, promotional, scam, adult, news, meme"],\n'
            '  "suspicious_indicators": ["any red flags: fake engagement, banned hashtags, suspicious links, impersonation, etc."],\n'
            '  "recommended_action": "1 sentence on how this affects reporting strategy",\n'
            '  "confidence": <float 0.0-1.0>\n'
            "}\n\n"
            "Reply ONLY with valid JSON. No markdown, no explanation."
        )

        # Build a vision-capable message if the provider supports image input.
        # Token Club / gpt-5.5 accept image URLs in message content.
        try:
            raw = self.gemini._ask_vision(prompt, image_data_uri=data_uri, max_tokens=600)
        except AttributeError:
            # Fallback: text-only prompt mentioning the image path
            raw = self.gemini._ask(
                f"{prompt}\n\n[Image reference: {filepath} - vision analysis not available via this provider]",
                max_tokens=600,
            )

        result = ScreenshotAnalysis(filepath=filepath)
        if raw:
            parsed = _extract_json_dict(raw)
            if parsed and isinstance(parsed, dict):
                result.description = str(parsed.get("description", result.description))
                result.post_types_detected = list(parsed.get("post_types_detected", []))
                result.suspicious_indicators = list(parsed.get("suspicious_indicators", []))
                result.recommended_action = str(parsed.get("recommended_action", result.recommended_action))
                result.confidence = float(parsed.get("confidence", result.confidence))
                result.raw_ai_response = raw.strip()
            else:
                result.description = raw.strip()
                result.confidence = 0.5

        self.screenshot_analyses.append(result)
        return result

    # ─── Interactive Chat ─────────────────────────────────────────────────────

    def chat(self, user_message: str) -> str:
        """Send a free-text message to the AI and record the exchange."""
        self.conversation_history.append({"role": "user", "content": user_message})

        # Build conversation context
        history_text = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in self.conversation_history[-10:]
        )

        system_prompt = (
            "You are an AI Sentinel conducting a pre-execution consultation for a TikTok "
            "moderation reporting campaign. The operator will describe posts they've seen "
            "on the target profile, upload screenshots, and ask questions. Your job:\n"
            "1. Identify what types of posts exist (dance, prank, political, promotional, "
            "scam, adult, news, meme, etc.)\n"
            "2. Explain which post types may violate TikTok Community Guidelines\n"
            "3. Recommend appropriate report vectors, pacing, and session strategy\n"
            "4. Be concise and practical\n\n"
            "Reply directly to the operator's message. Keep it under 200 words."
        )

        full_prompt = f"{system_prompt}\n\n--- CONVERSATION ---\n{history_text}"

        raw = self.gemini._ask(full_prompt, max_tokens=500)
        ai_response = raw.strip() if raw else "No response from AI."

        self.conversation_history.append({"role": "assistant", "content": ai_response})
        return ai_response

    # ─── Final Consultation Report ────────────────────────────────────────────

    def finalize(
        self,
        target_username: str,
        profile_context: Dict[str, Any],
        available_vectors: List[tuple],
    ) -> ConsultationReport:
        """Generate the final consultation report from all gathered info."""

        # Build a summary of everything the AI has seen
        screenshot_summaries = []
        for sa in self.screenshot_analyses:
            screenshot_summaries.append(
                f"  • {os.path.basename(sa.filepath)}: {sa.description} "
                f"(types: {', '.join(sa.post_types_detected) or 'none'}, "
                f"confidence: {sa.confidence:.0%})"
            )

        chat_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:200]}"
            for m in self.conversation_history
        )

        vector_labels = [f"{v[0]}:{v[2][:30]}" for v in available_vectors]

        prompt = (
            "You are completing a pre-execution consultation for a TikTok reporting campaign.\n"
            "Based on ALL information gathered (screenshots, operator chat, profile context), "
            "produce a FINAL structured recommendation. Reply ONLY with valid JSON:\n\n"
            '{\n'
            '  "risk_assessment": "Low|Medium|High|Critical",\n'
            '  "post_types_identified": ["list of post types found across all sources"],\n'
            '  "recommended_vectors": ["report vector codes that match the post types"],\n'
            '  "avoid_vectors": ["vector codes that do NOT match or would be ineffective"],\n'
            '  "pacing_recommendation": "string (e.g. \\"8-20s randomized jitter\\")",\n'
            '  "session_rotation": <bool>,\n'
            '  "device_mutation": <bool>,\n'
            '  "ai_reasoning": "2-4 sentences explaining the decision rationale",\n'
            '  "confidence_score": <float 0.0-1.0>\n'
            "}\n\n"
            "--- PROFILE CONTEXT ---\n"
            f"  Username: @{target_username}\n"
            f"  Followers: {profile_context.get('followers', '—')}\n"
            f"  Following: {profile_context.get('following', '—')}\n"
            f"  Likes: {profile_context.get('likes', '—')}\n"
            f"  Private: {profile_context.get('private', '—')}\n"
            f"  Bio: {str(profile_context.get('bio', '—'))[:200]}\n\n"
            "--- SCREENSHOT ANALYSES ---\n"
            f"{chr(10).join(screenshot_summaries) if screenshot_summaries else '  (none)'}\n\n"
            "--- OPERATOR CHAT ---\n"
            f"{chat_text if chat_text else '  (none)'}\n\n"
            "--- AVAILABLE VECTORS ---\n"
            f"{', '.join(vector_labels)}\n\n"
            "Reply ONLY with valid JSON. No markdown, no explanation."
        )

        raw = self.gemini._ask(prompt, max_tokens=800)

        report = ConsultationReport(target_username=target_username)
        report.screenshot_analyses = list(self.screenshot_analyses)
        report.chat_summary = chat_text[:500] if chat_text else ""

        if raw:
            parsed = _extract_json_dict(raw)
            if parsed and isinstance(parsed, dict):
                report.risk_assessment = str(parsed.get("risk_assessment", report.risk_assessment))
                report.post_types_identified = list(parsed.get("post_types_identified", []))
                report.recommended_vectors = list(parsed.get("recommended_vectors", []))
                report.avoid_vectors = list(parsed.get("avoid_vectors", []))
                report.pacing_recommendation = str(parsed.get("pacing_recommendation", report.pacing_recommendation))
                report.session_rotation = bool(parsed.get("session_rotation", report.session_rotation))
                report.device_mutation = bool(parsed.get("device_mutation", report.device_mutation))
                report.ai_reasoning = str(parsed.get("ai_reasoning", report.ai_reasoning))
                report.confidence_score = float(parsed.get("confidence_score", report.confidence_score))
                report.raw_ai_response = raw.strip()
            else:
                report.ai_reasoning = raw.strip()
                report.confidence_score = 0.5

        return report


# ──────────────────────────────────────────────────────────────────────────────
# CLI Runner
# ──────────────────────────────────────────────────────────────────────────────

def run_consultation_cli(
    consultation: PreflightConsultation,
    target_username: str,
    profile_context: Dict[str, Any],
    available_vectors: List[tuple],
) -> ConsultationReport:
    """
    Interactive CLI loop for the pre-flight consultation phase.

    The operator can:
      • Type messages to chat with the AI
      • Type 'screen <path>' to analyze a screenshot
      • Type 'done' to finalize and get the consultation report
      • Type 'skip' to bypass consultation entirely
    """
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    console = Console()

    console.print()
    console.print(Panel(
        "[bold cyan]PRE-FLIGHT AI CONSULTATION[/bold cyan]\n"
        "Upload screenshots, describe posts, and let the AI recommend "
        "the right vectors, pacing, and session strategy before execution.",
        border_style="cyan",
    ))
    console.print()
    console.print("[dim]Commands:[/dim]")
    console.print("  [green]screen <image_path>[/green]  - Analyze a screenshot")
    console.print("  [green]<message>[/green]           - Chat with the AI")
    console.print("  [green]done[/green]                 - Finalize and get report")
    console.print("  [green]skip[/green]                 - Skip consultation")
    console.print()

    while True:
        try:
            user_input = console.input("[bold yellow]Consult>[/bold yellow] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Consultation cancelled.[/yellow]")
            return ConsultationReport(target_username=target_username)

        if not user_input:
            continue

        cmd = user_input.lower()

        if cmd == "skip":
            console.print("[yellow]Consultation skipped.[/yellow]")
            return ConsultationReport(target_username=target_username)

        if cmd == "done":
            console.print("[cyan]Finalizing consultation...[/cyan]")
            report = consultation.finalize(
                target_username=target_username,
                profile_context=profile_context,
                available_vectors=available_vectors,
            )
            _print_report(console, report)
            return report

        if cmd.startswith("screen "):
            filepath = user_input[len("screen "):].strip().strip('"')
            if not filepath:
                console.print("[red]Please provide an image path.[/red]")
                continue
            if not os.path.exists(filepath):
                console.print(f"[red]File not found: {filepath}[/red]")
                continue
            console.print(f"[dim]Analyzing screenshot: {filepath}[/dim]")
            analysis = consultation.analyze_screenshot(filepath)
            console.print(f"[green]✓[/green] {analysis.description}")
            if analysis.post_types_detected:
                console.print(f"  Post types: {', '.join(analysis.post_types_detected)}")
            if analysis.suspicious_indicators:
                console.print(f"  [red]⚠[/red] Indicators: {', '.join(analysis.suspicious_indicators)}")
            console.print(f"  Recommended: {analysis.recommended_action}")
            console.print(f"  Confidence: {analysis.confidence:.0%}")
            continue

        # Regular chat message
        response = consultation.chat(user_input)
        console.print(f"\n[cyan]AI:[/cyan] {response}\n")

    return ConsultationReport(target_username=target_username)


def _print_report(console: "Console", report: ConsultationReport) -> None:
    """Render the final consultation report."""
    from rich.table import Table

    console.print()
    console.print(Panel(
        f"[bold green]CONSULTATION REPORT[/bold green]\n"
        f"Target: @{report.target_username}\n"
        f"Confidence: {report.confidence_score:.0%}",
        border_style="green",
    ))

    if report.post_types_identified:
        console.print(f"\n[bold]Post Types Identified:[/bold] {', '.join(report.post_types_identified)}")

    if report.recommended_vectors:
        console.print(f"[bold green]✓ Recommended Vectors:[/bold green] {', '.join(report.recommended_vectors)}")

    if report.avoid_vectors:
        console.print(f"[bold red]✗ Avoid Vectors:[/bold red] {', '.join(report.avoid_vectors)}")

    console.print(f"\n[bold]Risk Assessment:[/bold] {report.risk_assessment}")
    console.print(f"[bold]Pacing:[/bold] {report.pacing_recommendation}")
    console.print(f"[bold]Session Rotation:[/bold] {'Yes' if report.session_rotation else 'No'}")
    console.print(f"[bold]Device Mutation:[/bold] {'Yes' if report.device_mutation else 'No'}")

    if report.ai_reasoning:
        console.print(f"\n[bold]AI Reasoning:[/bold] {report.ai_reasoning}")

    if report.screenshot_analyses:
        console.print(f"\n[bold]Screenshot Analyses ({len(report.screenshot_analyses)}):[/bold]")
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("File", style="cyan")
        table.add_column("Description", style="white")
        table.add_column("Types", style="yellow")
        table.add_column("Conf.", justify="right")
        for sa in report.screenshot_analyses:
            table.add_row(
                os.path.basename(sa.filepath),
                sa.description[:60] + ("…" if len(sa.description) > 60 else ""),
                ", ".join(sa.post_types_detected) or "—",
                f"{sa.confidence:.0%}",
            )
        console.print(table)

    console.print()