"""CLI entry point for the financial market simulation framework."""

from __future__ import annotations
import argparse
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from orchestrator import Orchestrator
from utils.llm_client import LLMError, PROVIDER_NAMES

console = Console()


def print_banner():
    banner = Text()
    banner.append("╔══════════════════════════════════════════════════╗\n", style="bold cyan")
    banner.append("║   Financial Market Simulation Engine             ║\n", style="bold cyan")
    banner.append("║   Multi-Agent AI Framework                       ║\n", style="bold cyan")
    banner.append("╚══════════════════════════════════════════════════╝\n", style="bold cyan")
    console.print(banner)


def print_status(orch: Orchestrator) -> bool:
    """Show what is actually working. Returns False if the model cannot be used."""
    usable = True
    for line in orch.status_lines():
        if line.startswith("LLM PROBLEM"):
            usable = False
            console.print(f"[red]{line}[/red]")
        elif "OFF" in line:
            console.print(f"[yellow]{line}[/yellow]")
        else:
            console.print(f"[dim]{line}[/dim]")
    return usable


def run_simulation(orch: Orchestrator, scenario_text: str, economy: str = "US", preferences: dict | None = None):
    """Run a single simulation and print results."""

    def on_phase(phase: str, detail: str = ""):
        if detail:
            stripped = detail.strip()
            if stripped.startswith("✓"):
                console.print(f"  [green]{detail}[/green]")
            elif stripped.startswith("○") or "WARNING" in detail:
                console.print(f"  [yellow]{detail}[/yellow]")
            else:
                console.print(f"  [dim]{detail}[/dim]")
        else:
            console.print(f"[bold yellow]{phase}[/bold yellow]")

    def on_start(name: str):
        console.print(f"  [dim]▸ Running {name}...[/dim]")

    def on_done(name: str):
        console.print(f"  [green]✓ {name} complete[/green]")

    console.print()
    console.print(Panel(scenario_text, title="Scenario Input", border_style="blue"))
    console.print()
    console.print(
        "[dim]  ▸[/dim] in progress   "
        "[green]✓[/green] complete   "
        "[yellow]○[/yellow] skipped   "
        "[bold yellow]Phase[/bold yellow] stage header"
    )
    console.print()

    report = orch.run(
        scenario_text,
        base_economy=economy,
        parallel=True,
        preferences=preferences,
        on_agent_start=on_start,
        on_agent_done=on_done,
        on_phase=on_phase,
    )

    console.print()
    console.print(Markdown(report.to_markdown()))
    console.print()
    console.print(f"[dim]{orch.llm.usage.summary()}[/dim]")

    return report


def run_comparison(orch: Orchestrator, scenario_a: str, scenario_b: str, economy: str = "US"):
    """Run two simulations and print a comparison."""
    console.print()
    console.print("[bold magenta]Running Scenario A...[/bold magenta]")
    console.print(Panel(scenario_a, title="Scenario A", border_style="blue"))
    report_a = orch.run(scenario_a, base_economy=economy, parallel=True)
    console.print("[green]✓ Scenario A complete[/green]\n")

    console.print("[bold magenta]Running Scenario B...[/bold magenta]")
    console.print(Panel(scenario_b, title="Scenario B", border_style="blue"))
    report_b = orch.run(scenario_b, base_economy=economy, parallel=True)
    console.print("[green]✓ Scenario B complete[/green]\n")

    console.print("[bold yellow]Generating comparative analysis...[/bold yellow]")
    comparison = orch._compare_reports(report_a, report_b)
    console.print()
    console.print(Markdown(comparison))
    console.print()
    console.print(f"[dim]{orch.llm.usage.summary()}[/dim]")


def parse_preferences(pref_str: str) -> dict:
    """Parse a preferences string like 'depth=deep,focus=equity_market,forex'."""
    prefs = {}
    if not pref_str:
        return prefs
    for part in pref_str.split(","):
        part = part.strip()
        if "=" in part:
            key, val = part.split("=", 1)
            key = key.strip()
            val = val.strip()
            if key == "focus":
                prefs.setdefault("focus_domains", []).append(val)
            elif key == "depth":
                prefs["depth"] = val
            elif key == "horizon":
                prefs["time_horizon"] = val
            elif key == "no_debate":
                prefs["skip_debate"] = True
        elif part == "no_debate":
            prefs["skip_debate"] = True
        elif part:
            prefs.setdefault("focus_domains", []).append(part)
    return prefs


def interactive_mode(orch: Orchestrator):
    """Run in interactive REPL mode."""
    console.print("[dim]Type a policy scenario to simulate. Type 'quit' to exit.[/dim]")
    console.print("[dim]Type 'compare' to compare two scenarios side-by-side.[/dim]")
    console.print("[dim]Type 'prefs <options>' to set preferences (e.g. 'prefs depth=deep,focus=equity_market')[/dim]")
    console.print()

    preferences = {}

    while True:
        try:
            user_input = console.input("[bold cyan]scenario>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            console.print("[dim]Goodbye.[/dim]")
            break

        if user_input.lower().startswith("prefs"):
            pref_str = user_input[5:].strip()
            if pref_str:
                preferences = parse_preferences(pref_str)
                console.print(f"[green]Preferences set: {preferences}[/green]")
            else:
                preferences = {}
                console.print("[dim]Preferences cleared.[/dim]")
            continue

        if user_input.lower() == "compare":
            console.print("[dim]Enter Scenario A:[/dim]")
            a = console.input("[bold cyan]  A>[/bold cyan] ").strip()
            console.print("[dim]Enter Scenario B:[/dim]")
            b = console.input("[bold cyan]  B>[/bold cyan] ").strip()
            if a and b:
                try:
                    run_comparison(orch, a, b)
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")
            continue

        try:
            run_simulation(orch, user_input, preferences=preferences if preferences else None)
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")

        console.print()


def main():
    parser = argparse.ArgumentParser(description="Financial Market Simulation Engine")
    parser.add_argument("--scenario", "-s", type=str, help="Run a single scenario")
    parser.add_argument("--compare", action="store_true", help="Compare two scenarios")
    parser.add_argument("--scenario-a", type=str, help="First scenario for comparison")
    parser.add_argument("--scenario-b", type=str, help="Second scenario for comparison")
    parser.add_argument("--economy", "-e", type=str, default="US", help="Base economy (default: US)")
    parser.add_argument("--provider", type=str, default=None, choices=PROVIDER_NAMES,
                        help="LLM provider (default: LLM_PROVIDER from .env)")
    parser.add_argument("--model", "-m", type=str, default=None, help="Override the main model name")
    parser.add_argument("--preferences", "-p", type=str, default=None, help="Preferences (e.g. 'depth=deep,focus=equity_market')")
    parser.add_argument("--api-key", type=str, default=None, help="API key for a hosted provider (overrides .env)")

    args = parser.parse_args()

    try:
        orch = Orchestrator(api_key=args.api_key, model=args.model, provider=args.provider)
    except LLMError as e:
        console.print(f"[red]{e}[/red]")
        console.print("[dim]Check LLM_PROVIDER and its settings in .env (see .env.example).[/dim]")
        sys.exit(1)

    print_banner()
    if not print_status(orch):
        sys.exit(1)

    prefs = parse_preferences(args.preferences) if args.preferences else None

    if args.compare:
        if not args.scenario_a or not args.scenario_b:
            console.print("[red]--compare requires --scenario-a and --scenario-b[/red]")
            sys.exit(1)
        run_comparison(orch, args.scenario_a, args.scenario_b, args.economy)
    elif args.scenario:
        run_simulation(orch, args.scenario, args.economy, preferences=prefs)
    else:
        interactive_mode(orch)


if __name__ == "__main__":
    main()
