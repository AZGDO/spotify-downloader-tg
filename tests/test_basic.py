import asyncio

import pytest


@pytest.mark.asyncio
async def test_placeholder() -> None:
    await asyncio.sleep(0)
    assert True
