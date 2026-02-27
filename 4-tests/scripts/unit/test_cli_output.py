"""Unit tests for CLI output module."""

import json
import pytest
from io import StringIO
from unittest.mock import patch


class TestOutput:
    def test_print_json(self, capsys):
        from cli.output import print_json
        data = {"key": "值", "num": 42}
        print_json(data)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["key"] == "值"
        assert parsed["num"] == 42

    def test_print_text(self, capsys):
        from cli.output import print_text
        rows = [["a", "b"], ["c", "d"]]
        print_text(rows)
        captured = capsys.readouterr()
        lines = captured.out.strip().split("\n")
        assert len(lines) == 2
        assert "a\tb" in lines[0]

    def test_render_json_mode(self, capsys):
        from cli.output import render
        render("json", data={"status": "ok"})
        captured = capsys.readouterr()
        assert json.loads(captured.out)["status"] == "ok"

    def test_render_text_mode(self, capsys):
        from cli.output import render
        render("text", rows=[["x", "y"]])
        captured = capsys.readouterr()
        assert "x\ty" in captured.out

    def test_render_table_mode(self):
        from cli.output import render
        render("table", title="Test", columns=["A", "B"], rows=[["1", "2"]])
