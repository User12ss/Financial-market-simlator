"""CLI entry point for the financial market simulation framework."""

from __future__ import annotations
import argparse
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.live import Live
from rich.table import Table
from rich.text import Text

from orchestrator import Orchestrator

console = Console()


def print_banner():
    banner = Text()
    banner.append("╔══════════════════════════════════════════════════╗\n", style="bold cyan")
    banner.append("║   Financial Market Simulation Engine             ║\n", style="bold cyan")
    banner.append("║   Multi-Agent AI Framework                       ║\n", style="bold cyan")
    banner.append("╚══════════════════════════════════════════════════╝\n", style="bold cyan")
    console.print(banner)


def run_simulation(orch: Orchestrator, scenario_text: str, economy: str = "US"):
    """Run a single simulation and print results."""
    active_agents: list[str] = []

    def on_start(name: str):
        active_agents.append(name)
        console.print(f"  [dim]▸ Running {name}...[/dim]")

    def on_done(name: str):
        console.print(f"  [green]✓ {name} complete[/green]")

    console.print()
    console.print(Panel(scenario_text, title="Scenario Input", border_style="blue"))
    console.print()

    # Phase 0: Parse
    console.print("[bold yellow]Phase 0:[/bold yellow] Parsing scenario...")
    # Phase 1: Agents
    console.print("[bold yellow]Phase 1:[/bold yellow] Running agent analyses...")
    # Phase 2: Synthesis
    # (all handled inside orch.run)

    report = orch.run(
        scenario_text,
        base_economy=economy,
        parallel=True,
        on_agent_start=on_start,
        on_agent_done=on_done,
    )

    console.print()
    console.print("[bold yellow]Phase 2:[/bold yellow] Synthesizing cross-domain report...")
    console.print("[green]✓ Synthesis complete[/green]")
    console.print()

    # Print the report
    md = Markdown(report.to_markdown())
    console.print(md)

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


def interactive_mode(orch: Orchestrator):
    """Run in interactive REPL mode."""
    print_banner()
    console.print("[dim]Type a policy scenario to simulate. Type 'quit' to exit.[/dim]")
    console.print("[dim]Type 'compare' to compare two scenarios side-by-side.[/dim]")
    console.print()

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
            run_simulation(orch, user_input)
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
    parser.add_argument("--model", "-m", type=str, default=None, help="Override Claude model name")
    parser.add_argument("--api-key", type=str, default=None, help="Anthropic API key (overrides .env)")

    args = parser.parse_args()

    try:
        orch = Orchestrator(api_key=args.api_key, model=args.model)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        console.print("[dim]Set ANTHROPIC_API_KEY in .env or pass --api-key[/dim]")
        sys.exit(1)

    if args.compare:
        if not args.scenario_a or not args.scenario_b:
            console.print("[red]--compare requires --scenario-a and --scenario-b[/red]")
            sys.exit(1)
        run_comparison(orch, args.scenario_a, args.scenario_b, args.economy)
    elif args.scenario:
        run_simulation(orch, args.scenario, args.economy)
    else:
        interactive_mode(orch)


if __name__ == "__main__":
    main()
