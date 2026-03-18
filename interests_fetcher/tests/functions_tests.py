from datetime import timedelta
from interests_fetcher import functions
from interests_fetcher.data import settings
import datetime
import unittest


class TestCase(unittest.TestCase):

    #@unittest.SkipTest
    def test_pack_unzip(self):
        started = datetime.datetime.now()
        input_dir = settings.INPUT_FOLDER
        output_dir = settings.OUTPUT_FOLDER
        functions.unzip_archives_in_directory(input_dir, output_dir)
        functions.convert_and_concatenate_videos(output_dir)
        last = datetime.datetime.now() - started
        print("Last:", last.seconds)

    @unittest.SkipTest
    def test_download_video(self):
        time_start = datetime.datetime.strptime(
            "2025.01.29 17:45:00", "%Y.%m.%d %H:%M:%S")
        time_stop = datetime.datetime.strptime(
            "2025.01.29 17:46:00", "%Y.%m.%d %H:%M:%S")
        response = functions.download_video(
            time_start=time_start,
            time_stop=time_stop,
            device_id="104040",
            channel=1,
            destination_folder=settings.INPUT_FOLDER)
        print(response)

    @unittest.SkipTest
    def test_download_video_batch(self):
        interval = timedelta(minutes=2)
        times_list = functions.split_time_range_to_dicts(
            start_time_str="2025.01.29 17:40:00",
            end_time_str="2025.01.29 18:40:00",
            interval=interval)
        ranges = len(times_list)
        print(f"Ranges: {ranges}")
        started = datetime.datetime.now()
        for time_dict in times_list:
            print(f"Now - {times_list.index(time_dict)}/{ranges}")
            functions.download_video(
                time_start=time_dict["time_start"],
                time_stop=time_dict["time_end"],
                device_id="104040",
                channel=1,
                destination_folder=settings.INPUT_FOLDER)
        last = datetime.datetime.now() - started
        print("Last:", last.seconds)

    @unittest.SkipTest
    def test_download_test_route(self):
        started = datetime.datetime.now()
        global d
        ranges = len(d)
        for time_dict in d:
            print(f"Now - {d.index(time_dict)}/{ranges}")
            start_time = datetime.datetime.fromisoformat(
                time_dict["start_time"][:-1])
            stop_time = datetime.datetime.fromisoformat(
                time_dict["stop_time"][:-1])
            functions.download_video(
                time_start=start_time,
                time_stop=stop_time,
                device_id="104040",
                channel=1,
                destination_folder=settings.INPUT_FOLDER)
        last = datetime.datetime.now() - started
        print("Downloading.Last:", last.seconds)
        started = datetime.datetime.now()
        input_dir = settings.INPUT_FOLDER
        output_dir = settings.OUTPUT_FOLDER
        functions.unzip_archives_in_directory(input_dir, output_dir)
        functions.convert_and_concatenate_videos(output_dir)
        last = datetime.datetime.now() - started
        print("PvP.Last:", last.seconds)


d = [
    {
        "start_time": "2025-01-31T15:20:46Z",
        "stop_time": "2025-01-31T15:23:47Z",
        "lng": 0,
        "lat": 0
    },
    {
        "start_time": "2025-01-31T15:29:35Z",
        "stop_time": "2025-01-31T15:30:05Z",
        "lng": 55.985095,
        "lat": 54.746408
    },
    {
        "start_time": "2025-01-31T15:31:35Z",
        "stop_time": "2025-01-31T15:32:21Z",
        "lng": 55.983,
        "lat": 54.747208
    },
    {
        "start_time": "2025-01-31T15:33:36Z",
        "stop_time": "2025-01-31T15:34:52Z",
        "lng": 55.984707,
        "lat": 54.74901
    },
    {
        "start_time": "2025-01-31T15:35:52Z",
        "stop_time": "2025-01-31T15:37:38Z",
        "lng": 55.985736,
        "lat": 54.749344
    },
    {
        "start_time": "2025-01-31T15:38:08Z",
        "stop_time": "2025-01-31T15:38:23Z",
        "lng": 55.987324,
        "lat": 54.74891
    }
]

if __name__ == "__main__":
    unittest.main()

# 2939 seconds downloading
