"""Unit tests for PipeWire camera detection (no PipeWire daemon needed)."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../usr/share/biglinux/bigcam')))

from core.backends.pipewire_backend import PipeWireBackend


def _node(node_id, **props):
    return {"id": node_id, "type": "PipeWire:Interface:Node", "info": {"props": props}}


def _detect(*nodes):
    return PipeWireBackend()._parse_pw_objects(json.dumps(list(nodes)))


# Props captured from a real xdg-desktop-portal-wlr screen share (OBS).
XDPW_SCREENCAST = {
    "media.class": "Video/Source",
    "media.name": "xdpw-stream-lKgMML",
    "node.name": "xdg-desktop-portal-wlr",
    "stream.is-live": True,
}


def test_screencast_nodes_are_not_cameras():
    cameras = _detect(
        _node(187, **XDPW_SCREENCAST),
        _node(201, **{"media.class": "Video/Source", "node.name": "gnome-shell-screen-cast-1"}),
        _node(202, **{"media.class": "Video/Source", "node.name": "plasma.screencast"}),
        _node(203, **{"media.class": "Video/Source", "node.name": "share", "media.role": "Screen"}),
    )
    assert cameras == []


def test_virtual_cameras_are_still_detected():
    cameras = _detect(
        _node(187, **XDPW_SCREENCAST),
        _node(90, **{"media.class": "Video/Source/Virtual", "node.name": "obs",
                     "node.description": "OBS Virtual Camera"}),
    )
    assert [c.id for c in cameras] == ["pipewire:90"]
    assert cameras[0].name == "OBS Virtual Camera"


def test_v4l2_hardware_nodes_are_left_to_v4l2_backend():
    cameras = _detect(
        _node(50, **{"media.class": "Video/Source", "device.api": "v4l2",
                     "api.v4l2.path": "/dev/video0", "node.name": "v4l2_input.usb"}),
    )
    assert cameras == []
