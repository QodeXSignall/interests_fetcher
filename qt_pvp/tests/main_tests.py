import unittest
import asyncio

from main_operator import Main


class TestCase(unittest.TestCase):

    def test_get_pics_before_after(self):
        async def runner():
            d = Main()
            return await d.get_channels_to_download_pics(
                "/Tracker/Видео выгрузок/К180КЕ702/2025.10.08/К180КЕ702_2025.10.08 06.23.33-06.24"
            )

        res = asyncio.run(runner())
        print("\nRES", res)


if __name__ == "__main__":
    unittest.main()