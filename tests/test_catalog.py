import unittest

from bot.keyboards import PAGE_SIZE, build_manufacturer_keyboard, build_model_keyboard
from car.catalog import MANUFACTURERS, models_for
from car.car_search_filter import CarSearchFilter


class CatalogTests(unittest.TestCase):
    def test_manufacturer_pages_cover_catalog_without_exceeding_telegram_limits(self):
        pages = (len(MANUFACTURERS) + PAGE_SIZE - 1) // PAGE_SIZE
        shown_ids = set()
        for page in range(pages):
            keyboard = build_manufacturer_keyboard(set(), page)
            buttons = [button for row in keyboard.inline_keyboard for button in row]
            self.assertLessEqual(len(buttons), 24)
            shown_ids.update(
                int(button.callback_data.split(":")[1])
                for button in buttons
                if button.callback_data.startswith("mfr_toggle:")
            )
        self.assertEqual(shown_ids, {manufacturer["id"] for manufacturer in MANUFACTURERS})
        self.assertEqual(len(shown_ids), 127)
        self.assertIn(62, shown_ids)  # Tesla
        self.assertIn(18, shown_ids)  # Volvo
        self.assertIn(369, shown_ids)  # Omoda

    def test_models_follow_selected_manufacturer(self):
        tesla_models = models_for({62})
        self.assertIn(10846, {model["id"] for model in tesla_models})  # Model 3
        keyboard = build_model_keyboard(set(), {62})
        shown = {
            int(button.callback_data.split(":")[1])
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data.startswith("mdl_toggle:")
        }
        self.assertEqual(shown, {model["id"] for model in tesla_models})
        self.assertNotIn(10711, shown)  # Kia Picanto

    def test_selected_ids_are_encoded_in_yad2_url_parameters(self):
        car_filter = CarSearchFilter(manufacturers=[18, 62], models=[10846])
        self.assertEqual(car_filter.get_url_parameters()[:2], ["manufacturer=18,62", "model=10846"])


if __name__ == "__main__":
    unittest.main()
