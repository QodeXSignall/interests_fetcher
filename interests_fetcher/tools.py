from main_operator import Main
from interests_fetcher.functions import get_reg_info

def get_interests(reg_id, time_start, time_end):
    t = Main()
    return t.get_interests(reg_id, get_reg_info(reg_id), time_start, time_end)

if __name__ == "__main__":
    reg_id = "018270348452"
    #time_start = "2025-05-11 08:13:10"
    #time_end = "2025-05-11 18:13:20"
    time_start = "2025-06-29 14:15:20"
    time_end = "2025-06-29 14:45:20"
    interests = get_interests(reg_id, time_start, time_end)
    for int in interests:
        print(int)
