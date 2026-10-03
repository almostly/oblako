"""Unit tests: Fargate task sizes are refused as AWS refuses them."""

from __future__ import annotations

import pytest

from oblako.services.ecs import fargate_size_error


def _td(cpu, memory, compat=("FARGATE",)):
    return {"requiresCompatibilities": list(compat), "cpu": cpu, "memory": memory}


@pytest.mark.parametrize(
    ("cpu", "memory"),
    [
        ("256", "512"),
        ("256", "2048"),
        ("1024", "8192"),
        ("1 vCPU", "2 GB"),
        (4096, 30720),
    ],
)
def test_valid_sizes_pass(cpu, memory):
    assert fargate_size_error(_td(cpu, memory)) is None


@pytest.mark.parametrize(
    ("cpu", "memory"),
    [
        ("256", "1536"),
        ("256", "4096"),
        ("512", "512"),
        ("300", "1024"),
        ("8192", "18432"),
    ],
)
def test_invalid_sizes_are_refused(cpu, memory):
    error = fargate_size_error(_td(cpu, memory))
    assert error and error.startswith(
        "No Fargate configuration exists for given values"
    )


def test_ec2_task_definitions_are_not_checked():
    assert fargate_size_error(_td("256", "4096", compat=("EC2",))) is None


def test_fargate_needs_task_level_cpu():
    assert fargate_size_error({"requiresCompatibilities": ["FARGATE"]})
