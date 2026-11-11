# Copyright (c) 2026 Chrys. All rights reserved.

"""Pier's own mini adapter, activating an installed Compass Package per trial.

Runs on the orchestrator, not in the task container, so it is the only module
here that imports Pier. Everything about the measured scaffold stays inherited:
the pinned mini version, the provider backend choice, ``-c mini.yaml`` and the
ATIF trajectory conversion.

A trial container is not an Installation Host: it exists for one trial and is
destroyed after it, with no CodeHelix Home and no PackageStore. What it does have
is the same activation shape the installer uses -- one ``.pth`` in the
interpreter that runs mini, pointing at an immutable Package. So the Package is
staged into the trial's own agent log directory, which Pier already binds into
the container, and activated there per trial as an external ``RuntimeBinding``
that CodeHelix does not own.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from pier.agents.installed.mini_swe_agent import MiniSweAgent

DEFAULT_AGENT_CLASS = "compass_brief.mini.agent.CompassMiniAgent"
BASELINE_AGENT_CLASS = "compass_brief.mini.submission.SubmissionSafeMiniAgent"
ACTIVATION = "codehelix_compass_brief.pth"
STAGED = "compass-package"


class CompassMiniSweAgent(MiniSweAgent):
    """Official mini scaffold with a Compass session maintained alongside it."""

    def __init__(
        self,
        *args,
        compass_package: str,
        compass_agent_class: str = DEFAULT_AGENT_CLASS,
        compass_container_logs: str = "/logs/agent",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        #: Host path of the Installed Package, whose ``package/src`` the activation
        #: points at -- the same content the installer would register.
        self._compass_package = Path(compass_package)
        self._compass_agent_class = compass_agent_class
        #: Where Pier mounts this trial's agent log directory inside the container.
        self._compass_container_logs = compass_container_logs.rstrip("/")
        # Selecting the agent class here rather than in the caller's YAML is what
        # makes this class *mean* "mini with Compass". A forgotten key would run
        # the baseline and still be compared as though it were the treatment.
        config = yaml.safe_load(self._config_yaml) if self._config_yaml else {}
        config = config if isinstance(config, dict) else {}
        agent = config.setdefault("agent", {})
        agent["agent_class"] = compass_agent_class
        # Maintenance must reach the same provider through litellm, and mini's native
        # backends strip the provider prefix from the model name they keep -- an
        # OpenRouter run leaves `deepseek/...`, which litellm reads as DeepSeek
        # direct and cannot authenticate. Measured: extraction failed with
        # provider_transient and silently fell back to the regex contract, so the
        # arm recorded Evidence and maintained nothing. Pier's own model name still
        # carries the prefix, so pass that unless the caller named one.
        if compass_agent_class != BASELINE_AGENT_CLASS:
            agent.setdefault("compass_maintenance_model", self.model_name or "")
        self._config_yaml = yaml.safe_dump(config, sort_keys=False, allow_unicode=True)

    # name() stays inherited. The install spec is byte-identical to plain mini's,
    # so both arms share one cached derived image; the arm is identified by the
    # agent import path and by ``info.config.agent_type`` in the trajectory.

    async def setup(self, environment) -> None:
        await super().setup(environment)
        # Staging through Pier's own log bind rather than an extra volume is what
        # keeps its default mounts intact: replacing them removed `/logs/agent`,
        # and mini's `tee` into it then failed the whole run. It also archives the
        # exact Package a trial used next to that trial's results.
        staged = Path(self.logs_dir) / STAGED / "src"
        if not staged.exists():
            origin = self._compass_package / "package/src"
            package = origin / "compass_brief"
            if not package.is_dir():
                raise RuntimeError(f"No compass_brief package under {origin}")
            staged.mkdir(parents=True)
            # Only the package and its distribution metadata. A Delivery also
            # carries the optional Lingxi scenario unit, which has no part in a
            # mini trial and would be copied into all 113 of them.
            for item in (package, *sorted(origin.glob("compass_brief-*.dist-info"))):
                shutil.copytree(item, staged / item.name)
        source = f"{self._compass_container_logs}/{STAGED}/src"
        result = await self.exec_as_agent(
            environment,
            command=(
                '. "$HOME/.local/bin/env"\n'
                f'test -d {source} || {{ echo "Package not staged at {source}" >&2; exit 1; }}\n'
                'python_bin="$(head -n 1 "$(command -v mini-swe-agent)" | sed \'s/^#!//\')"\n'
                'purelib="$("$python_bin" -c \'import sysconfig;print(sysconfig.get_path("purelib"))\')"\n'
                f'printf "%s\\n" {source} > "$purelib/{ACTIVATION}"\n'
                '"$python_bin" -c "'
                "import sys; from importlib.metadata import version; import compass_brief; "
                "from minisweagent.agents import get_agent_class; "
                f'assert get_agent_class({self._compass_agent_class!r}) is not None; '
                "print('compass-brief', version('compass-brief'), 'from', compass_brief.__file__, "
                "'on', sys.version.split()[0])\"\n"
            ),
        )
        if getattr(result, "return_code", 0):
            # Continuing would run plain mini and report it as the treatment.
            raise RuntimeError(
                f"Compass Package from {self._compass_package} did not activate in mini's "
                f"interpreter: {getattr(result, 'output', '')[-2000:]}"
            )

    def populate_context_post_run(self, context) -> None:
        """Charge the trial for Compass's own calls as well as mini's.

        mini counts only its main conversation, and the inherited implementation
        copies exactly that. Compass's task extraction and Claim maintenance are
        separate provider calls the trial really paid for, so a comparison that
        reported only mini's share would understate this arm. The meter rides out
        of the container inside mini's own trajectory, which Pier already reads.
        """
        super().populate_context_post_run(context)
        trajectory = self.logs_dir / "mini-swe-agent.trajectory.json"
        if not trajectory.is_file():
            return
        try:
            compass = json.loads(trajectory.read_text())["info"]["compass"]
            side = compass["side_calls"]["total"]
        except (OSError, ValueError, KeyError, TypeError):
            self.logger.debug("No Compass side-call meter in the mini trajectory")
            return
        context.n_input_tokens = (context.n_input_tokens or 0) + int(side.get("input_tokens") or 0)
        context.n_output_tokens = (context.n_output_tokens or 0) + int(side.get("output_tokens") or 0)
        if side.get("cost_usd") is None:
            # Unknown side-call cost makes the total unknown; a stale mini-only
            # figure would read as the whole bill.
            context.cost_usd = None
        elif context.cost_usd is not None:
            context.cost_usd += float(side["cost_usd"])
        # The split stays readable beside the trial, because a single total cannot
        # show how much of it Compass added.
        (self.logs_dir / "compass-side-calls.json").write_text(
            json.dumps(compass["side_calls"], indent=2), encoding="utf-8"
        )


class BaselineMiniSweAgent(CompassMiniSweAgent):
    """Same staged package and submission guard, with no Compass session or calls."""

    def __init__(self, *args, **kwargs):
        kwargs["compass_agent_class"] = BASELINE_AGENT_CLASS
        super().__init__(*args, **kwargs)
