from __future__ import annotations

import datetime
from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from aura.drones.contracts import TYPE_NAMES
from aura.drones.definition import DroneBudget, DroneDefinition
from aura.drones.store import DroneStore
from aura.gui.theme import ACCENT, BG, BG_ALT, BG_RAISED, BORDER, FG, FG_DIM


class DroneEditorDialog(QDialog):
    """Modal dialog for editing an installed Drone manifest."""

    def __init__(
        self,
        workspace_root: Path,
        parent: QWidget | None = None,
        drone: DroneDefinition | None = None,
        *,
        initial_name: str = "",
        initial_instructions: str = "",
        initial_output_contract: str = "",
        initial_write_policy: str = "read_only",
    ) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._drone = drone

        self.setWindowTitle("Drone Manifest")
        self.setMinimumWidth(480)
        self.setModal(True)
        self.setStyleSheet(
            f"QDialog {{ background: {BG_ALT}; }}"
        )

        self._build_ui()
        if drone is not None:
            self._populate(drone)

        # Apply initial values (for pre-fill from Save-as-Drone)
        if initial_name:
            self._name_edit.setText(initial_name)
        if initial_instructions:
            self._instructions_edit.setPlainText(initial_instructions)
        if initial_output_contract:
            self._output_contract_edit.setPlainText(initial_output_contract)
        if initial_write_policy:
            idx = self._policy_combo.findData(initial_write_policy)
            if idx >= 0:
                self._policy_combo.setCurrentIndex(idx)

        if drone is not None:
            self._set_accepts_produces(drone)

    def _set_accepts_produces(self, drone: DroneDefinition) -> None:
        if drone.accepts:
            idx = self._accepts_combo.findText(drone.accepts)
            if idx >= 0:
                self._accepts_combo.setCurrentIndex(idx)
            else:
                self._accepts_combo.setCurrentText(drone.accepts)
        if drone.produces:
            idx = self._produces_combo.findText(drone.produces)
            if idx >= 0:
                self._produces_combo.setCurrentIndex(idx)
            else:
                self._produces_combo.setCurrentText(drone.produces)

    # -- Public API --

    @property
    def drone(self) -> DroneDefinition | None:
        return self._drone

    # -- UI construction --

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Name
        name_label = QLabel("Name")
        name_label.setStyleSheet(f"color: {FG}; font-weight: 600; font-size: 13px;")
        layout.addWidget(name_label)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. Release Checklist")
        self._name_edit.setStyleSheet(
            f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 5px; padding: 6px 8px; color: {FG};"
        )
        layout.addWidget(self._name_edit)

        # Instructions
        instr_label = QLabel("What should this Drone do?")
        instr_label.setStyleSheet(f"color: {FG}; font-weight: 600; font-size: 13px;")
        layout.addWidget(instr_label)
        self._instructions_edit = QPlainTextEdit()
        self._instructions_edit.setPlaceholderText(
            "Describe the task the Drone should perform..."
        )
        self._instructions_edit.setMinimumHeight(80)
        self._instructions_edit.setStyleSheet(
            f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 5px; padding: 6px 8px; color: {FG};"
        )
        layout.addWidget(self._instructions_edit)

        # Write policy
        policy_label = QLabel("Can it edit files?")
        policy_label.setStyleSheet(f"color: {FG}; font-weight: 600; font-size: 13px;")
        layout.addWidget(policy_label)
        self._policy_combo = QComboBox()
        self._policy_combo.addItem("No, read-only", "read_only")
        self._policy_combo.addItem("Ask before writes", "ask_before_writes")
        self._policy_combo.addItem("Normal diff approval", "normal_diff_approval")
        self._policy_combo.setStyleSheet(
            f"QComboBox {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 5px; padding: 5px 8px; color: {FG}; }}"
        )
        layout.addWidget(self._policy_combo)

        # Accepts (input contract)
        accepts_label = QLabel("Accepts (input contract)")
        accepts_label.setStyleSheet(
            f"color: {FG}; font-weight: 600; font-size: 13px;"
        )
        layout.addWidget(accepts_label)
        self._accepts_combo = QComboBox()
        self._accepts_combo.setEditable(True)
        self._accepts_combo.addItem("(any)", "")
        for t in TYPE_NAMES:
            self._accepts_combo.addItem(t, t)
        self._accepts_combo.setCurrentIndex(0)
        self._accepts_combo.setStyleSheet(
            f"QComboBox {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 5px; padding: 5px 8px; color: {FG}; }}"
        )
        layout.addWidget(self._accepts_combo)

        # Produces (output contract)
        produces_label = QLabel("Produces (output contract)")
        produces_label.setStyleSheet(
            f"color: {FG}; font-weight: 600; font-size: 13px;"
        )
        layout.addWidget(produces_label)
        self._produces_combo = QComboBox()
        self._produces_combo.setEditable(True)
        self._produces_combo.addItem("(any)", "")
        for t in TYPE_NAMES:
            self._produces_combo.addItem(t, t)
        self._produces_combo.setCurrentIndex(0)
        self._produces_combo.setStyleSheet(
            f"QComboBox {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 5px; padding: 5px 8px; color: {FG}; }}"
        )
        layout.addWidget(self._produces_combo)

        # Output contract
        ocontract_label = QLabel("What should it bring back?")
        ocontract_label.setStyleSheet(
            f"color: {FG}; font-weight: 600; font-size: 13px;"
        )
        layout.addWidget(ocontract_label)
        self._output_contract_edit = QPlainTextEdit()
        self._output_contract_edit.setPlaceholderText(
            "Describe the expected output format..."
        )
        self._output_contract_edit.setMinimumHeight(60)
        self._output_contract_edit.setStyleSheet(
            f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 5px; padding: 6px 8px; color: {FG};"
        )
        layout.addWidget(self._output_contract_edit)

        # Budget section
        budget_header = QLabel("Budget")
        budget_header.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 700; font-size: 12px; "
            f"text-transform: uppercase; letter-spacing: 0.04em; "
            f"padding: 8px 0 0 0;"
        )
        layout.addWidget(budget_header)

        budget_row = QHBoxLayout()
        budget_row.setSpacing(16)

        # Max tool rounds
        rounds_label = QLabel("Max tool rounds:")
        rounds_label.setStyleSheet(f"color: {FG}; font-size: 12px;")
        budget_row.addWidget(rounds_label)
        self._rounds_spin = QSpinBox()
        self._rounds_spin.setRange(1, 50)
        self._rounds_spin.setValue(8)
        self._rounds_spin.setStyleSheet(
            f"QSpinBox {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 4px; padding: 4px; color: {FG}; }}"
        )
        budget_row.addWidget(self._rounds_spin)

        # Timeout
        timeout_label = QLabel("Timeout seconds:")
        timeout_label.setStyleSheet(f"color: {FG}; font-size: 12px;")
        budget_row.addWidget(timeout_label)
        self._timeout_spin = QSpinBox()
        self._timeout_spin.setRange(30, 3600)
        self._timeout_spin.setValue(300)
        self._timeout_spin.setStyleSheet(
            f"QSpinBox {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 4px; padding: 4px; color: {FG}; }}"
        )
        budget_row.addWidget(self._timeout_spin)
        budget_row.addStretch(1)

        layout.addLayout(budget_row)

        # First Run Test
        frt_label = QLabel("First Run Test (optional)")
        frt_label.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 700; font-size: 12px; "
            f"text-transform: uppercase; letter-spacing: 0.04em; "
            f"padding: 8px 0 0 0;"
        )
        layout.addWidget(frt_label)

        self._first_run_test_edit = QLineEdit()
        self._first_run_test_edit.setPlaceholderText("e.g. run_diagnostic_command echo hello")
        self._first_run_test_edit.setStyleSheet(
            f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 5px; padding: 6px 8px; color: {FG};"
        )
        layout.addWidget(self._first_run_test_edit)

        # Buttons
        btn_box = QDialogButtonBox()
        save_btn = QPushButton("Save Drone")
        save_btn.setObjectName("primary")
        save_btn.setStyleSheet(
            f"QPushButton#primary {{ background: {ACCENT}; color: {BG}; "
            f"border: 1px solid {ACCENT}; border-radius: 6px; "
            f"padding: 6px 20px; font-weight: 600; }}"
        )
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; "
            f"padding: 6px 20px; }}"
        )
        btn_box.addButton(save_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        btn_box.addButton(cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        btn_box.accepted.connect(self._on_save)
        btn_box.rejected.connect(self.reject)

        layout.addWidget(btn_box)

    def _populate(self, drone: DroneDefinition) -> None:
        self._name_edit.setText(drone.name)
        self._instructions_edit.setPlainText(drone.instructions)
        self._output_contract_edit.setPlainText(drone.output_contract)

        idx = self._policy_combo.findData(drone.write_policy)
        if idx >= 0:
            self._policy_combo.setCurrentIndex(idx)

        self._rounds_spin.setValue(drone.budget.max_tool_rounds)
        self._timeout_spin.setValue(drone.budget.timeout_seconds)
        self._first_run_test_edit.setText(drone.first_run_test)
        self._set_accepts_produces(drone)

    @staticmethod
    def _combo_value(combo: QComboBox) -> str:
        data = combo.currentData()
        if data is not None:
            return data
        text = combo.currentText().strip()
        return "" if text == "(any)" else text

    # -- Save logic --

    def _on_save(self) -> None:
        name = self._name_edit.text().strip()
        instructions = self._instructions_edit.toPlainText().strip()
        output_contract = self._output_contract_edit.toPlainText().strip()
        write_policy = self._policy_combo.currentData()
        first_run_test = self._first_run_test_edit.text().strip()

        if not name:
            QMessageBox.warning(self, "Validation", "Name is required.")
            self._name_edit.setFocus()
            return
        if not instructions:
            QMessageBox.warning(self, "Validation", "Instructions are required.")
            self._instructions_edit.setFocus()
            return
        if not output_contract:
            QMessageBox.warning(self, "Validation", "Output contract is required.")
            self._output_contract_edit.setFocus()
            return

        # Derive description from instructions (first sentence, ~200 chars max)
        description = self._derive_description(instructions)

        budget = DroneBudget(
            max_tool_rounds=self._rounds_spin.value(),
            timeout_seconds=self._timeout_spin.value(),
        )
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if self._drone is None:
            QMessageBox.warning(
                self,
                "Drone Architect",
                "New Drones must be built with /drone so Aura can create the folder, code, manifest, and smoke check.",
            )
            return

        self._drone = replace(
            self._drone,
            name=name,
            description=description,
            instructions=instructions,
            write_policy=write_policy,
            output_contract=output_contract,
            first_run_test=first_run_test,
            budget=budget,
            updated_at=now,
            accepts=self._combo_value(self._accepts_combo),
            produces=self._combo_value(self._produces_combo),
        )

        DroneStore.save_drone(self._workspace_root, self._drone)
        self.accept()

    @staticmethod
    def _derive_description(instructions: str) -> str:
        """Take the first sentence from instructions, capped at ~200 chars."""
        # Split on sentence boundary or newline
        for sep in (". ", "\n", "."):
            if sep in instructions:
                first = instructions.split(sep, 1)[0]
                break
        else:
            first = instructions

        first = first.strip()
        if len(first) > 200:
            first = first[:197] + "..."
        return first
