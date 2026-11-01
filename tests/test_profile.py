import pytest

from modbus_sim.device import Device
from modbus_sim.profile import ProfileError, bundled_profiles, load_profile, profile_from_dict


def base(*points, **extra):
    return {"name": "t", "points": list(points), **extra}


def test_bundled_profiles_load_and_build():
    assert bundled_profiles() == ["eastron-sdm630", "energy-meter", "omron-e5cc",
                                  "sunspec-inverter", "temperature-controller", "vfd"]
    for name in bundled_profiles():
        Device(load_profile(name))


@pytest.mark.parametrize("point, message", [
    ({"name": "a", "table": "nope", "address": 0}, "table must be"),
    ({"name": "a", "table": "holding", "address": 70000}, "address"),
    ({"name": "a", "table": "coil", "address": 0, "type": "uint16"}, "type bool"),
    ({"name": "a", "table": "holding", "address": 0, "type": "string"}, "length"),
    ({"name": "a", "table": "input", "address": 0, "access": "rw"}, "cannot be written"),
    ({"name": "a", "table": "holding", "address": 65535, "type": "float32"}, "does not fit"),
    ({"name": "a", "table": "holding", "address": 0, "scale": 0}, "scale"),
    ({"name": "a", "table": "holding", "address": 0, "colour": "red"}, "unknown key"),
    ({"name": "a", "table": "holding", "address": 0, "source": {"type": "sine"}}, "takes"),
    ({"name": "a", "table": "holding", "address": 0, "word_order": "ADBC"}, "word_order"),
    ({"name": "a", "table": "holding", "address": 0, "model": "x"}, "names none"),
])
def test_invalid_points(point, message):
    with pytest.raises(ProfileError, match=message):
        profile_from_dict(base(point))


def test_overlap_and_duplicates():
    with pytest.raises(ProfileError, match="already used"):
        profile_from_dict(base({"name": "a", "table": "holding", "address": 0, "type": "int32"},
                               {"name": "b", "table": "holding", "address": 1}))
    with pytest.raises(ProfileError, match="duplicate"):
        profile_from_dict(base({"name": "a", "table": "holding", "address": 0},
                               {"name": "a", "table": "holding", "address": 1}))
    # the same address in different tables is fine
    profile_from_dict(base({"name": "a", "table": "holding", "address": 0},
                           {"name": "b", "table": "input", "address": 0}))


def test_overrides():
    p = load_profile("energy-meter", {"word_order": "ABCD", "values": {"label": "HALL"},
                                      "model_params": {"voltage": 120},
                                      "identification": {"product": "X1"}})
    assert p.point("voltage_l1").word_order == "ABCD"
    assert p.point("label").value == "HALL"
    assert p.model_params["voltage"] == 120 and p.model_params["frequency"] == 50.0
    assert p.identification["product"] == "X1" and p.identification["vendor"] == "steitec"
    with pytest.raises(ProfileError, match="driven by"):
        load_profile("vfd", {"values": {"status_word": 3}})
    with pytest.raises(ProfileError, match="unknown override"):
        load_profile("vfd", {"colour": "red"})


def test_profile_file(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("name: x\npoints: [{name: a, table: holding, address: 0}]\n")
    assert load_profile(str(path)).name == "x"
    path.write_text("name: [\n")
    with pytest.raises(ProfileError, match="invalid YAML"):
        load_profile(str(path))
