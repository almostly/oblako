"""Unit tests for GlueService (config only; the live job test needs Docker + the image)."""

from oblako.services.glue import IMAGE_TAG, GlueService


def test_image_tag():
    assert IMAGE_TAG == "amazon/aws-glue-libs:5"


def test_glue_attached_to_platform():
    from oblako.services.platform import Oblako

    o = Oblako()
    assert isinstance(o.glue, GlueService)
    assert o.glue.name == "glue"
