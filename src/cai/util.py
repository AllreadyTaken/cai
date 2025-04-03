"""
Util model for CAI
"""
import os
import sys
import importlib.resources
import pathlib
from rich.console import Console
from rich.tree import Tree
from mako.template import Template  # pylint: disable=import-error
from wasabi import color
from rich.text import Text  # pylint: disable=import-error
from rich.panel import Panel  # pylint: disable=import-error
from rich.box import ROUNDED  # pylint: disable=import-error
from rich.theme import Theme  # pylint: disable=import-error
from rich.traceback import install  # pylint: disable=import-error
from rich.pretty import install as install_pretty  # pylint: disable=import-error # noqa: 501
from datetime import datetime

theme = Theme({
    # Primary colors - Material Design inspired
    "timestamp": "#00BCD4",  # Cyan 500
    "agent": "#4CAF50",      # Green 500
    "arrow": "#FFFFFF",      # White
    "content": "#ECEFF1",    # Blue Grey 50
    "tool": "#F44336",       # Red 500

    # Secondary colors
    "cost": "#009688",        # Teal 500
    "args_str": "#FFC107",  # Amber 500

    # UI elements
    "border": "#2196F3",      # Blue 500
    "border_state": "#FFD700",      # Yellow (Gold), complementary to Blue 500
    "model": "#673AB7",       # Deep Purple 500
    "dim": "#9E9E9E",         # Grey 500
    "current_token_count": "#E0E0E0",  # Grey 300 - Light grey
    "total_token_count": "#757575",    # Grey 600 - Medium grey
    "context_tokens": "#0A0A0A",       # Nearly black - Very high contrast

    # Status indicators
    "success": "#4CAF50",     # Green 500
    "warning": "#FF9800",     # Orange 500
    "error": "#F44336"        # Red 500
})

console = Console(theme=theme)
install()
install_pretty()


def get_ollama_api_base():
    """Get the Ollama API base URL from environment variable or default to localhost:8000."""
    return os.environ.get("OLLAMA_API_BASE", "http://localhost:8000/v1")

def load_prompt_template(template_path):
    """
    Load a prompt template from the package resources.
    
    Args:
        template_path: Path to the template file relative to the cai package,
                      e.g., "prompts/system_bug_bounter.md"
    
    Returns:
        The rendered template as a string
    """
    try:
        # Get the template file from package resources
        template_path_parts = template_path.split('/')
        package_path = ['cai'] + template_path_parts[:-1]
        package = '.'.join(package_path)
        filename = template_path_parts[-1]
        
        # Read the content from the package resources
        # Handle different importlib.resources APIs between Python versions
        try:
            # Python 3.9+ API
            template_content = importlib.resources.read_text(package, filename)
        except (TypeError, AttributeError):
            # Fallback for Python 3.8 and earlier
            with importlib.resources.path(package, filename) as path:
                template_content = pathlib.Path(path).read_text(encoding='utf-8')
        
        # Render the template
        return Template(template_content).render()
    except Exception as e:
        raise ValueError(f"Failed to load template '{template_path}': {str(e)}")

def visualize_agent_graph(start_agent):
    """
    Visualize agent graph showing all bidirectional connections between agents.
    Uses Rich library for pretty printing.
    """
    console = Console()  # pylint: disable=redefined-outer-name
    if start_agent is None:
        console.print("[red]No agent provided to visualize.[/red]")
        return

    tree = Tree(
        f"🤖 {
            start_agent.name} (Current Agent)",
        guide_style="bold blue")

    # Track visited agents and their nodes to handle cross-connections
    visited = {}
    agent_nodes = {}
    agent_positions = {}  # Track positions in tree
    position_counter = 0  # Counter for tracking positions

    def add_agent_node(agent, parent=None, is_transfer=False):  # pylint: disable=too-many-branches # noqa: E501
        """Add agent node and track for cross-connections"""
        nonlocal position_counter

        if agent is None:
            return None

        # Create or get existing node for this agent
        if id(agent) in visited:
            if is_transfer:
                # Add reference with position for repeated agents
                original_pos = agent_positions[id(agent)]
                parent.add(
                    f"[cyan]↩ Return to {
                        agent.name} (Top Level Agent #{original_pos})[/cyan]")
            return agent_nodes[id(agent)]

        visited[id(agent)] = True
        position_counter += 1
        agent_positions[id(agent)] = position_counter

        # Create node for current agent
        if is_transfer:
            node = parent
        else:
            node = parent.add(
                f"[green]{agent.name} (#{position_counter})[/green]") if parent else tree  # noqa: E501 pylint: disable=line-too-long
        agent_nodes[id(agent)] = node

        # Add tools as children
        tools_node = node.add("[yellow]Tools[/yellow]")
        for fn in getattr(agent, "functions", []):
            if callable(fn):
                fn_name = getattr(fn, "__name__", "")
                if ("handoff" not in fn_name.lower() and
                        not fn_name.startswith("transfer_to")):
                    tools_node.add(f"[blue]{fn_name}[/blue]")

        # Add Handoffs section
        transfers_node = node.add("[magenta]Handoffs[/magenta]")

        # Process handoff functions
        for fn in getattr(agent, "functions", []):  # pylint: disable=too-many-nested-blocks # noqa: E501
            if callable(fn):
                fn_name = getattr(fn, "__name__", "")
                if ("handoff" in fn_name.lower() or
                        fn_name.startswith("transfer_to")):
                    try:
                        next_agent = fn()
                        if next_agent:
                            # Show bidirectional connection
                            transfer = transfers_node.add(
                                f"🤖 {next_agent.name}")  # noqa: E501
                            add_agent_node(next_agent, transfer, True)
                    except Exception:  # nosec: B112 # pylint: disable=broad-exception-caught # noqa: E501
                        continue
        return node
    # Start recursive traversal from root agent
    add_agent_node(start_agent)
    console.print(tree)

def fix_litellm_transcription_annotations():
    """
    Apply a monkey patch to fix the TranscriptionCreateParams.__annotations__ issue in LiteLLM.
    
    This is a temporary fix until the issue is fixed in the LiteLLM library itself.
    """
    try:
        import litellm.litellm_core_utils.model_param_helper as model_param_helper
        
        # Override the problematic method to avoid the error
        original_get_transcription_kwargs = model_param_helper.ModelParamHelper._get_litellm_supported_transcription_kwargs
        
        def safe_get_transcription_kwargs():
            """A safer version that doesn't rely on __annotations__."""
            return set(["file", "model", "language", "prompt", "response_format", 
                       "temperature", "api_base", "api_key", "api_version", 
                       "timeout", "custom_llm_provider"])
        
        # Apply the monkey patch
        model_param_helper.ModelParamHelper._get_litellm_supported_transcription_kwargs = safe_get_transcription_kwargs        
        return True
    except (ImportError, AttributeError):
        # If the import fails or the attribute doesn't exist, the patch couldn't be applied
        return False

def fix_message_list(messages):  # pylint: disable=R0914,R0915,R0912
    """
    Sanitizes the message list passed as a parameter to align with the
    OpenAI API message format.

    Adjusts the message list to comply with the following rules:
        1. A tool call id appears no more than twice.
        2. Each tool call id appears as a pair, and both messages
            must have content.
        3. If a tool call id appears alone (without a pair), it is removed.
        4. There cannot be empty messages.

    Args:
        messages (List[dict]): List of message dictionaries containing
                            role, content, and optionally tool_calls or
                            tool_call_id fields.

    Returns:
        List[dict]: Sanitized list of messages with invalid tool calls
                   and empty messages removed.
    """
    # Step 1: Filter and discard empty messages (considered empty if 'content'
    # is None or only whitespace)
    cleaned_messages = []
    for msg in messages:
        content = msg.get("content")
        if content is not None and content.strip():
            cleaned_messages.append(msg)
    messages = cleaned_messages
    # Step 2: Collect tool call id occurrences.
    # In assistant messages, iterate through 'tool_calls' list.
    # In 'tool' type messages, use the 'tool_call_id' key.
    tool_calls_occurrences = {}
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and isinstance(
                msg.get("tool_calls"), list):
            for j, tool_call in enumerate(msg["tool_calls"]):
                tc_id = tool_call.get("id")
                if tc_id:
                    tool_calls_occurrences.setdefault(
                        tc_id, []).append((i, "assistant", j))
        elif msg.get("role") == "tool" and msg.get("tool_call_id"):
            tc_id = msg["tool_call_id"]
            tool_calls_occurrences.setdefault(
                tc_id, []).append((i, "tool", None))

    # Step 3: Mark indices in the message list to remove.
    # Maps message index (assistant) to set of indices (in tool_calls) to
    # delete, or directly marks message indices (tool) to delete.
    to_remove = {}
    for tc_id, occurrences in tool_calls_occurrences.items():
        if len(occurrences) > 2:
            # More than one assistant and tool message pair - trim down
            # by picking first pairing and removing the rest
            assistant_items = [
                occ for occ in occurrences if occ[1] == "assistant"]
            tool_items = [occ for occ in occurrences if occ[1] == "tool"]

            if assistant_items and tool_items:
                valid_assistant = assistant_items[0]
                valid_tool = tool_items[0]
                for item in occurrences:
                    if item != valid_assistant and item != valid_tool:
                        if item[1] == "assistant":
                            # If assistant message, mark specific tool_call index
                            to_remove.setdefault(item[0], set()).add(item[2])
                        else:
                            # If tool message, mark whole message
                            to_remove[item[0]] = None
            else:
                # Only one type of message, no complete pairs - remove them all
                for item in occurrences:
                    if item[1] == "assistant":
                        to_remove.setdefault(item[0], set()).add(item[2])
                    else:
                        to_remove[item[0]] = None
        elif len(occurrences) == 1:
            # Incomplete pair (only tool call without tool result or vice versa)
            item = occurrences[0]
            if item[1] == "assistant":
                to_remove.setdefault(item[0], set()).add(item[2])
            else:
                to_remove[item[0]] = None

    # Step 4: Apply the removals and reconstruct the message list
    sanitized_messages = []
    for i, msg in enumerate(messages):
        if i in to_remove and to_remove[i] is None:
            # Skip entirely removed messages
            continue

        # For assistant messages, remove marked tool_calls
        if msg.get("role") == "assistant" and "tool_calls" in msg:
            new_tool_calls = []
            for j, tc in enumerate(msg["tool_calls"]):
                if i not in to_remove or j not in to_remove[i]:
                    new_tool_calls.append(tc)
            msg["tool_calls"] = new_tool_calls
            # If after modification message has no content and no tool_calls,
            # skip it
            if not (msg.get("content", "").strip() or
                   not msg.get("tool_calls")):
                continue

        sanitized_messages.append(msg)

    return sanitized_messages

def cli_print_tool_call(tool_name="", args="", output="", prefix="  "):
    """Print a tool call with pretty formatting"""
    if not tool_name:
        return

    print(f"\n{prefix}{color('Tool Call:', fg='cyan')}")
    print(f"{prefix}{color('Name:', fg='cyan')} {tool_name}")
    if args:
        print(f"{prefix}{color('Args:', fg='cyan')} {args}")
    if output:
        print(f"{prefix}{color('Output:', fg='cyan')} {output}")

def get_model_input_tokens(model):
    """
    Get the number of input tokens for
    max context window capacity for a given model.
    """
    model_tokens = {
        "gpt": 128000,
        "o1": 200000,
        "claude": 200000,
        "qwen2.5": 32000,  # https://ollama.com/library/qwen2.5, 128K input, 8K output  # noqa: E501  # pylint: disable=C0301
        "llama3.1": 32000,  # https://ollama.com/library/llama3.1, 128K input  # noqa: E501  # pylint: disable=C0301
        "deepseek": 128000  # https://api-docs.deepseek.com/quick_start/pricing  # noqa: E501  # pylint: disable=C0301
    }
    for model_type, tokens in model_tokens.items():
        if model_type in model:
            return tokens
    return model_tokens["gpt"]

def _create_token_display(  # pylint: disable=too-many-arguments,too-many-locals,too-many-statements,too-many-branches # noqa: E501
    interaction_input_tokens,
    interaction_output_tokens,  # noqa: E501, pylint: disable=R0913
    interaction_reasoning_tokens,
    total_input_tokens,
    total_output_tokens,
    total_reasoning_tokens,
    model,
    interaction_cost=0.0,
    total_cost=None
) -> Text:  # noqa: E501
    """
    Create a Text object displaying token usage information
    with enhanced formatting.
    """
    tokens_text = Text(justify="left")

    # Create a more compact, horizontal display
    tokens_text.append(" ", style="bold")  # Small padding
    
    # Current interaction tokens
    tokens_text.append("Current: ", style="bold")
    tokens_text.append(f"I:{interaction_input_tokens} ", style="green")
    tokens_text.append(f"O:{interaction_output_tokens} ", style="red")
    tokens_text.append(f"R:{interaction_reasoning_tokens} ", style="yellow")
    
    # Current cost
    current_cost = float(interaction_cost) if interaction_cost is not None else 0.0
    tokens_text.append(f"(${current_cost:.4f}) ", style="bold")
    
    # Separator
    tokens_text.append("| ", style="dim")
    
    # Total tokens
    tokens_text.append("Total: ", style="bold")
    tokens_text.append(f"I:{total_input_tokens} ", style="green")
    tokens_text.append(f"O:{total_output_tokens} ", style="red")
    tokens_text.append(f"R:{total_reasoning_tokens} ", style="yellow")
    
    # Total cost
    total_cost_value = float(total_cost) if total_cost is not None else 0.0
    tokens_text.append(f"(${total_cost_value:.4f}) ", style="bold")
    
    # Separator
    tokens_text.append("| ", style="dim")
    
    # Context usage
    context_pct = interaction_input_tokens / get_model_input_tokens(model) * 100
    tokens_text.append("Context: ", style="bold")
    tokens_text.append(f"{context_pct:.1f}% ", style="bold")
    
    # Context indicator
    if context_pct < 50:
        indicator = "🟩"
        color_local = "green"
    elif context_pct < 80:
        indicator = "🟨"
        color_local = "yellow"
    else:
        indicator = "🟥"
        color_local = "red"
    
    tokens_text.append(f"{indicator}", style=color_local)

    return tokens_text

def cli_print_agent_messages(agent_name, message, counter, model, debug,  # pylint: disable=too-many-arguments,too-many-locals,unused-argument # noqa: E501
                             interaction_input_tokens=None,
                             interaction_output_tokens=None,
                             interaction_reasoning_tokens=None,
                             total_input_tokens=None,
                             total_output_tokens=None,
                             total_reasoning_tokens=None,
                             interaction_cost=None,
                             total_cost=None):
    """Print agent messages/thoughts with enhanced visual formatting."""
    # Use the model from environment variable if available
    model_override = os.getenv('CAI_MODEL')
    if model_override:
        model = model_override

    timestamp = datetime.now().strftime("%H:%M:%S")

    # Create a more hacker-like header
    text = Text()

    # Special handling for Reasoner Agent
    if agent_name == "Reasoner Agent":
        text.append(f"[{counter}] ", style="bold red")
        text.append(f"Agent: {agent_name} ", style="bold yellow")
        if message:
            text.append(f">> {message} ", style="green")
        text.append(f"[{timestamp}", style="dim")
        if model:
            text.append(f" ({os.getenv('CAI_SUPPORT_MODEL')})",
                        style="bold blue")
        text.append("]", style="dim")
    else:
        text.append(f"[{counter}] ", style="bold cyan")
        text.append(f"Agent: {agent_name} ", style="bold green")
        if message:
            text.append(f">> {message} ", style="yellow")
        text.append(f"[{timestamp}", style="dim")
        if model:
            text.append(f" ({model})", style="bold magenta")
        text.append("]", style="dim")

    # Add token information with enhanced formatting
    tokens_text = None
    if (interaction_input_tokens is not None and  # pylint: disable=R0916
            interaction_output_tokens is not None and
            interaction_reasoning_tokens is not None and
            total_input_tokens is not None and
            total_output_tokens is not None and
            total_reasoning_tokens is not None):

        tokens_text = _create_token_display(
            interaction_input_tokens,
            interaction_output_tokens,
            interaction_reasoning_tokens,
            total_input_tokens,
            total_output_tokens,
            total_reasoning_tokens,
            model,
            interaction_cost,
            total_cost
        )
        text.append(tokens_text)

    # Create a panel for better visual separation
    panel = Panel(
        text,
        border_style="red" if agent_name == "Reasoner Agent" else "blue",
        box=ROUNDED,
        padding=(0, 1),
        title=("[bold]Reasoning Analysis[/bold]"
               if agent_name == "Reasoner Agent"
               else "[bold]Agent Interaction[/bold]"),
        title_align="left"
    )
    console.print("\n")
    console.print(panel)

def create_agent_streaming_context(agent_name, counter, model):
    """Create a streaming context object that maintains state for streaming agent output."""
    from rich.live import Live
    import shutil
    
    # Use the model from environment variable if available
    model_override = os.getenv('CAI_MODEL')
    if model_override:
        model = model_override
        
    timestamp = datetime.now().strftime("%H:%M:%S")
    
    # Determine terminal size for best display
    terminal_width, _ = shutil.get_terminal_size((100, 24))
    panel_width = min(terminal_width - 4, 120)  # Keep some margin
    
    # Create base header for the panel
    header = Text()
    header.append(f"[{counter}] ", style="bold cyan")
    header.append(f"Agent: {agent_name} ", style="bold green")
    header.append(f">> ", style="yellow")
    
    # Create the content area for streaming text
    content = Text("")
    
    # Add timestamp and model info
    footer = Text()
    footer.append(f"\n[{timestamp}", style="dim")
    if model:
        footer.append(f" ({model})", style="bold magenta")
    footer.append("]", style="dim")
    
    # Create the panel (initial state)
    panel = Panel(
        Text.assemble(header, content, footer),
        border_style="blue",
        box=ROUNDED,
        padding=(1, 2),  # Add more padding for better readability
        title="[bold]Agent Streaming Response[/bold]",
        title_align="left",
        width=panel_width,
        expand=True  # Allow panel to expand to terminal width
    )
    
    # Start the live display with a higher refresh rate
    live = Live(panel, refresh_per_second=20, console=console)
    live.start()
    
    # Return context object with all the elements needed for updating
    return {
        "live": live,
        "panel": panel,
        "header": header,
        "content": content,
        "footer": footer,
        "timestamp": timestamp,
        "model": model,
        "agent_name": agent_name,
        "panel_width": panel_width
    }

def update_agent_streaming_content(context, text_delta):
    """Update the streaming content with new text."""
    # Add the new text to the content
    context["content"].append(text_delta)
    
    # Update the live display with the latest content
    updated_panel = Panel(
        Text.assemble(context["header"], context["content"], context["footer"]),
        border_style="blue",
        box=ROUNDED,
        padding=(1, 2),  # Match padding from creation
        title="[bold]Agent Streaming Response[/bold]",
        title_align="left",
        width=context.get("panel_width", 100),
        expand=True  # Allow panel to expand to terminal width
    )
    
    # Force an update with the new panel
    context["live"].update(updated_panel)
    context["panel"] = updated_panel

def finish_agent_streaming(context, final_stats=None):
    """Finish the streaming session and display final stats if available."""
    # If we have token stats, add them
    tokens_text = None
    if final_stats:
        interaction_input_tokens = final_stats.get("interaction_input_tokens")
        interaction_output_tokens = final_stats.get("interaction_output_tokens")
        interaction_reasoning_tokens = final_stats.get("interaction_reasoning_tokens")
        total_input_tokens = final_stats.get("total_input_tokens")
        total_output_tokens = final_stats.get("total_output_tokens")
        total_reasoning_tokens = final_stats.get("total_reasoning_tokens")
        interaction_cost = final_stats.get("interaction_cost")
        total_cost = final_stats.get("total_cost")
        
        if (interaction_input_tokens is not None and
                interaction_output_tokens is not None and
                interaction_reasoning_tokens is not None and
                total_input_tokens is not None and
                total_output_tokens is not None and
                total_reasoning_tokens is not None):
            
            tokens_text = _create_token_display(
                interaction_input_tokens,
                interaction_output_tokens,
                interaction_reasoning_tokens,
                total_input_tokens,
                total_output_tokens,
                total_reasoning_tokens,
                context["model"],
                interaction_cost,
                total_cost
            )
    
    # Create the final panel with stats
    final_panel = Panel(
        Text.assemble(
            context["header"], 
            context["content"], 
            Text("\n\n"), 
            tokens_text if tokens_text else Text(""),
            context["footer"]
        ),
        border_style="blue",
        box=ROUNDED,
        padding=(1, 2),  # Match padding from creation
        title="[bold]Agent Streaming Response[/bold]",
        title_align="left",
        width=context.get("panel_width", 100),
        expand=True
    )
    
    # Update one last time
    context["live"].update(final_panel)
    
    # Ensure updates are displayed before stopping
    import time
    time.sleep(0.5)
    
    # Stop the live display
    context["live"].stop()