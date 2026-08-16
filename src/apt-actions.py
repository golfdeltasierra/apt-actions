#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

from fnmatch import fnmatch
import json
import os
import argparse
import sys
import subprocess
from pathlib import Path
from dataclasses import dataclass, asdict, field, fields
from typing import List

def parse_args():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()

    group.add_argument("--parse", help="Parse package data from stdin", action="store_true")
    group.add_argument("--pre", help="Process pre-transaction actions", action="store_true")
    group.add_argument("--post", help="Process post-transaction actions", action="store_true")

    return parser.parse_args()

@dataclass()
class PackageTransaction:
    name: str
    old_version: str
    old_arch: str
    old_multiarch: str
    direction: str
    new_version: str
    new_arch: str
    new_multiarch: str
    raw_action: str
    operation: str = ""

PackageTransaction.FIELD_ORDER = tuple(f.name for f in fields(PackageTransaction))

@dataclass()
class Action:
    target: str
    phase: str
    transaction_type: str
    command: str

    def matching_packages(self, transaction_data, phase):
        if not fnmatch(self.phase, phase):
            return []
        
        return [
            pkg for pkg in transaction_data
            if fnmatch(pkg["operation"], self.transaction_type.upper())
            and fnmatch(pkg["name"], self.target)
        ]

    @staticmethod
    def _serialize_package(pkg: dict) -> str:
        return "\t".join(str(pkg[name]) for name in PackageTransaction.FIELD_ORDER)

    def run(self, packages):
        env = os.environ.copy()
        env["APT_ACTIONS_COUNT"] = str(len(packages))

        payload = "\n".join(self._serialize_package(p) for p in packages) + "\n"


        result = subprocess.run(self.command, 
        shell=True, 
        env=env, 
        executable="/bin/bash",
        input=payload)
        if result.returncode != 0:
            print(
                f"apt-actions: command exited with code {result.returncode}: {self.command}",
                file=sys.stderr
            )

@dataclass()
class Transaction:
    protocol: int = 3
    packages: List[PackageTransaction] = field(default_factory=list)

class AptActions:
    DEFAULT_CONFIG_DIR = Path("/etc/apt-actions")
    DEFAULT_RUNTIME_DIR = Path("/run/apt-actions")
    DEFAULT_ACTIONS_DIR = DEFAULT_CONFIG_DIR / "actions.d"
    SEPARATOR = ";"

    def __init__(self, config_dir=None, runtime_dir=None, actions_dir = None):
        self.config_dir = Path(config_dir or os.getenv("APT_ACTIONS_CONFIG_DIR") or self.DEFAULT_CONFIG_DIR)
        self.runtime_dir = Path(runtime_dir or os.getenv("APT_ACTIONS_RUNTIME_DIR") or self.DEFAULT_RUNTIME_DIR)
        self.actions_dir = Path(actions_dir or os.getenv("APT_ACTIONS_DIR") or self.DEFAULT_ACTIONS_DIR)
        self.tmp_file = self.runtime_dir / "tmp.json"

        self.transaction_data = []
        self.actions = []

        self.transaction_field_count = 9

    def _operation(self, pkg: PackageTransaction):
        if pkg.raw_action == "REMOVE":
            return "REMOVE"
        elif pkg.old_version == "-" and pkg.new_version != "-":
            return "INSTALL"
        elif pkg.direction == "<":
            return "UPGRADE"
        elif pkg.direction == ">":
            return "DOWNGRADE"
        elif pkg.old_version == pkg.new_version:
            return "REINSTALL"
        
        # unknown operation
        raise ValueError(
            f"Unable to determine package operation for '{pkg.name}' "
            f"raw_action = {pkg.raw_action} "
            f"direction = {pkg.direction} "
            f"old_version = {pkg.old_version} "
            f"new_version = {pkg.new_version}"
        )

    def parse(self):
        bulk_data = reversed(sys.stdin.readlines())

        transaction_data = Transaction()

        for line in bulk_data:
            line = line.rstrip()
            if line == "":
                break
            
            parts = line.split()
            if len(parts) != self.transaction_field_count:
                raise ValueError(
                    f"unexpected number of fields in apt transaction line "
                    f"expected {self.transaction_field_count}, got {len(parts)}: {line!r}"
                )

            pkg = PackageTransaction(*parts)

            if pkg.raw_action.endswith(".deb"): # ignore duplicate
                continue
            
            pkg.operation = self._operation(pkg)
            transaction_data.packages.append(pkg)
        
        # preserve original order
        transaction_data.packages = list(reversed(transaction_data.packages))

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        with open(self.tmp_file, "w") as json_file:
            json.dump(asdict(transaction_data), json_file, indent=2)

    def read_parsed_data(self):
        if not self.tmp_file.exists():
            raise SystemExit(
                f"apt-actions: {self.tmp_file} does not exist - "
                f"--parse should be run before --pre or --post"
            )
        try:
            with open(self.tmp_file, "r") as json_file:
                self.transaction_data = json.load(json_file)["packages"]
        except json.JSONDecodeError as e:
            raise SystemExit(f"apt-actions: {self.tmp_file} corrupted: {e}")


    def read_actions(self):
        if not self.actions_dir.is_dir():
            return

        valid_actions = sorted(
            item for item in self.actions_dir.iterdir()
            if item.is_file() and item.suffix == ".action"
        )
        
        for action_file in valid_actions:
            with open(action_file, "r") as action_data:
                for line_num, line in enumerate(action_data, start=1):
                    line = line.rstrip()

                    if not line or line.startswith("#"):
                        continue

                    parts = line.split(self.SEPARATOR, maxsplit=3)
                    if len(parts) != 4:
                        raise ValueError(
                            f"{action_file}:{line_num}: "
                            f"expected 4 fields separated by '{self.SEPARATOR}', "
                            f"got {len(parts)}"
                        )
                    
                    action = Action(*parts)
                    action.transaction_type = action.transaction_type.upper()
                    self.actions.append(action)


    def run_actions(self, current_phase: str):
        self.read_parsed_data()
        self.read_actions()

        for action in self.actions:
            matches = action.matching_packages(self.transaction_data, current_phase)
            if matches: # run command only once if there's a match, not one time per match
                action.run(matches)

        if current_phase == "post":
            self.tmp_file.unlink(missing_ok=True)

    def main(self, args):
        if args.parse:
            self.parse()
        
        elif args.pre:
            self.run_actions("pre")

        elif args.post:
            self.run_actions("post")


if __name__ == "__main__":
    AptActions().main(parse_args())
