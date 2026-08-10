"""Exercise runtime resources from an installed wheel rather than the source tree."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import aibb
from aibb.harness.runner import create_run_manifest
from aibb.protocol.world import CURRENT_STARTING_POINTS_VERSION, starting_points_path
from aibb.scaffold import create_board


def main() -> None:
    environment = Path(sys.prefix).resolve()
    package = Path(aibb.__file__).resolve()
    points = starting_points_path(CURRENT_STARTING_POINTS_VERSION).resolve()
    if not package.is_relative_to(environment) or not points.is_relative_to(environment):
        raise RuntimeError("wheel smoke test imported source-tree files")
    if not points.is_file():
        raise RuntimeError(f"installed starting-points resource is missing: {points}")

    with tempfile.TemporaryDirectory(prefix="aibb-wheel-smoke-") as temporary:
        root = Path(temporary)
        board = root / "board"
        create_board(destination=board)
        manifest, run_dir = create_run_manifest(
            data_repo=board,
            state_root=root / "state",
            model_id="example/model",
            display_name="Example Model",
            generation=None,
            lineage=None,
            mode="headless",
            compaction_policy="deny",
            contribution_quota=1,
            max_output_tokens=1_024,
            max_provider_turns=2,
            max_total_tokens=16_000,
            max_cost_usd=1,
            max_contributions_per_thread=1,
            model_context_window=32_000,
            model_max_completion_tokens=1_024,
            prompt_price_per_token=0,
            completion_price_per_token=0,
            allow_repeat_reason=None,
        )
        if not (run_dir / "manifest.json").is_file() or manifest.starting_points_sha256 is None:
            raise RuntimeError("installed wheel could not create a complete run manifest")

    print(f"installed aibb {aibb.__version__} run-manifest smoke passed")


if __name__ == "__main__":
    main()
