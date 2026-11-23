"""P0: контракт между Shop и Flow, без реальных приватных ключей."""
import copy
import unittest

from app.flow_subscription_contract import (
    SubscriptionContractError, build_document, compare_revision, validate_document
)

FAKE_CONFIG = "[Interface]\nPrivateKey = TEST_ONLY_NOT_A_REAL_KEY\nAddress = 10.0.0.2/32\n"


def active(revision=1):
    return build_document(
        revision=revision, state="active", expires_at=1893456000,
        profiles=[{"id": "device-a-peer-1", "name": "Основной сервер",
                   "protocol": "wireguard", "config": FAKE_CONFIG}]
    )


class TestFlowSubscriptionContract(unittest.TestCase):
    def test_active_document_and_round_trip(self):
        doc = active()
        self.assertEqual(doc["schema"], "pushok-flow-subscription")
        self.assertEqual(doc["version"], 1)
        self.assertIs(validate_document(doc), doc)

    def test_expired_and_revoked_have_no_profiles(self):
        for state in ("expired", "revoked"):
            doc = build_document(revision=2, state=state, expires_at=100, profiles=[])
            self.assertEqual(doc["profiles"], [])

    def test_reject_inactive_with_config(self):
        for state in ("expired", "revoked"):
            with self.assertRaises(SubscriptionContractError):
                build_document(revision=2, state=state, expires_at=100,
                               profiles=active()["profiles"])

    def test_reject_bad_revision_and_types(self):
        for revision in (0, -1, True, 1.2, "2"):
            with self.assertRaises(SubscriptionContractError):
                active(revision)

    def test_reject_duplicate_ids_and_oversize_catalogue(self):
        doc = active()
        doc["profiles"].append(copy.deepcopy(doc["profiles"][0]))
        with self.assertRaises(SubscriptionContractError):
            validate_document(doc)
        doc = active()
        doc["profiles"] = [
            {**doc["profiles"][0], "id": str(i)} for i in range(21)
        ]
        with self.assertRaises(SubscriptionContractError):
            validate_document(doc)

    def test_revision_replay_and_conflict(self):
        original = active(3)
        self.assertEqual(compare_revision(original, active(3)), "unchanged")
        self.assertEqual(compare_revision(original, active(2)), "stale")
        self.assertEqual(compare_revision(original, active(4)), "apply")
        changed = active(3)
        changed["expires_at"] += 30
        self.assertEqual(compare_revision(original, changed), "conflict")

    def test_revoked_cannot_be_rolled_back(self):
        revoked = build_document(revision=7, state="revoked",
                                 expires_at=100, profiles=[])
        self.assertEqual(compare_revision(revoked, active(6)), "stale")
        self.assertEqual(compare_revision(revoked, active(7)), "conflict")

    def test_reject_unknown_fields_without_echoing_config(self):
        doc = active()
        doc["admin_api_key"] = "secret-should-not-leak"
        with self.assertRaises(SubscriptionContractError) as caught:
            validate_document(doc)
        self.assertNotIn("secret-should-not-leak", str(caught.exception))

    def test_profile_identifier_stable_across_renewal(self):
        old = active(10)
        renewed = active(11)
        renewed["expires_at"] += 86400
        self.assertEqual(old["profiles"][0]["id"], renewed["profiles"][0]["id"])
        self.assertEqual(compare_revision(old, renewed), "apply")


if __name__ == "__main__":
    unittest.main()
