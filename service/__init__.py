"""FastAPI service layer that wraps the ppt-master skill into an HTTP API.

This package does NOT modify the ppt-master skill. It drives a headless
Claude Code agent (route A: CLI subprocess) that executes the SKILL.md
pipeline end-to-end and returns the exported .pptx.
"""
