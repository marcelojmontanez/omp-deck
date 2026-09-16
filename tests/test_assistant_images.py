import unittest

from omp_deck._fleet_projection import normalize_messages


class AssistantImageTests(unittest.TestCase):
    def test_portrait_reference_preserves_adjacent_prompt_and_native_image(self):
        digest = "a" * 64
        message = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"Portrait prompt\n\n![Envoy](blob:sha256:{digest})\nSettings"},
                {"type": "image", "data": f"blob:sha256:{'b' * 64}"},
            ],
        }
        result = normalize_messages([message], "portrait-session")
        self.assertEqual(
            result[0]["content"],
            [
                {"type": "text", "text": "Portrait prompt\n\n"},
                {"type": "image", "url": f"/api/blob/{digest}"},
                {"type": "text", "text": "Settings"},
                {"type": "image", "url": f"/api/blob/{'b' * 64}"},
            ],
        )

    def test_code_examples_and_untrusted_paths_remain_text(self):
        image = f"![Example](blob:sha256:{'a' * 64})"
        text = (
            f"````markdown\n```\n{image}\n````\n"
            f"~~~\n{image}\n~~~\n"
            f"    {image}\n"
            "![Local](/private/portrait.png)\n"
            "![External](https://example.com/portrait.png)\n"
            f"![Traversal](blob:sha256:{'a' * 64}/../secret)\n"
            f"![Uppercase](blob:sha256:{'A' * 64})\n"
        )
        result = normalize_messages([{"role": "assistant", "content": text}], "examples")
        self.assertEqual(result[0]["content"], [{"type": "text", "text": text}])

    def test_user_text_does_not_create_an_attachment(self):
        text = f"![Not an upload](blob:sha256:{'a' * 64})"
        result = normalize_messages([{"role": "user", "content": text}], "user-prompt")
        self.assertEqual(result[0]["content"], [{"type": "text", "text": text}])
