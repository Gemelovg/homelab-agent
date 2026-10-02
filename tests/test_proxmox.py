from homelab_agent.proxmox import sanitize_guest_config


def test_free_text_notes_are_withheld():
    # Regression: a real VM had a bare password in its notes, which pattern redaction missed.
    out = sanitize_guest_config({"description": "admin / Example-Pa55word", "cores": 4, "digest": "abc"})
    assert "Example-Pa55word" not in str(out)
    assert out["description"].startswith("[withheld")
    assert out["cores"] == "4"
    assert "digest" not in out


def test_other_fields_still_pattern_redacted():
    out = sanitize_guest_config({"cipassword": "x", "net0": "name=eth0,ip=dhcp"})
    assert out["net0"] == "name=eth0,ip=dhcp"
