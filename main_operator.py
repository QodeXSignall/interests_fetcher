from interests_fetcher.interest_merge_funcs import merge_overlapping_interests
from interests_fetcher.cms_interface import functions as cms_api_funcs
from interests_fetcher.functions import parse_interest_name
from interests_fetcher.qt_rm_client import QTRMAsyncClient
from interests_fetcher import functions as main_funcs
from interests_fetcher import cloud_uploader
import time

from interests_fetcher.logger import logger, pipeline_event
from interests_fetcher.data import settings
from interests_fetcher import cms_gate_client
from interests_fetcher import video_utils
import posixpath
import traceback
import datetime
import asyncio
import shutil
import os


class Main:
    def __init__(self, output_format="mp4"):
        #threading.Thread(target=main_funcs.video_remover_cycle).start()
        self.output_format = output_format
        self.devices_in_progress = []
        self.TIME_FMT = "%Y-%m-%d %H:%M:%S"
        self._global_interests_sem = None
        self._per_device_sem = {}
        self._devices_sem = None
        self._last_devices_online = []
        self._interest_refill_in_progress = set()
        self.qt_rm_client = QTRMAsyncClient(
            base_url=settings.qt_rm_url,
            username=settings.qt_rm_login,
            password=settings.qt_rm_password,
            concurrent_requests=settings.config.getint("QT_RM", "CONCURRENT_REQUESTS", fallback=16),)

    def _get_global_sem(self):
        if self._global_interests_sem is None:
            self._global_interests_sem = asyncio.Semaphore(settings.config.getint("Process", "MAX_GLOBAL_INTERESTS"))
        return self._global_interests_sem

    def _get_devices_sem(self):
        if self._devices_sem is None:
            self._devices_sem = asyncio.Semaphore(settings.config.getint("Process", "MAX_DEVICES_CONCURRENT"))
        return self._devices_sem

    def _get_device_sem(self, reg_id):
        sem = self._per_device_sem.get(reg_id)
        if sem is None:
            sem = asyncio.Semaphore(settings.config.getint("Process", "MAX_INTERESTS_PER_DEVICE"))
            self._per_device_sem[reg_id] = sem
        return sem

    async def _sync_recheck_with_cloud(
        self,
        reg_id: str,
        recheck_interests: list[dict],
        st: str,
        en: str,
        time_fmt: str,
    ) -> None:
        """
        Сравнивает интересы, найденные при recheck, с тем, что уже лежит на WebDAV:

          - берём интересы на облаке в окне [st, en]
          - фаззи-сравниваем их с recheck_interests
          - новые (detected, которых нет на облаке) -> append_pending_interests
          - устаревшие (на облаке, но нет в detected) -> удаляем с облака
        """

        if not recheck_interests:
            return

        # Окно для recheck
        try:
            window_start = datetime.datetime.strptime(st, time_fmt)
            window_end = datetime.datetime.strptime(en, time_fmt)
        except Exception as e:
            logger.warning(f"{reg_id}: Не удалось распарсить окно recheck [{st} → {en}]: {e}")
            return

        # Пытаемся достать plate. Надёжнее всего — из имени интереса.
        plate = None
        for it in recheck_interests:
            nm = (it or {}).get("name")
            if not nm:
                continue
            try:
                p, _, _ = main_funcs._interest_name_to_interval(nm)
                plate = p
                break
            except Exception:
                continue

        if not plate:
            # fallback: пробуем из states.json
            reg_cfg = main_funcs.get_reg_info(reg_id)
            plate = (reg_cfg or {}).get("plate")

        if not plate:
            logger.warning(f"{reg_id}: Не удалось определить plate для recheck-синхронизации. Пропускаем.")
            return

        client = cloud_uploader.client

        # Собираем имена интересов на облаке, которые попадают в окно [window_start, window_end]
        expected_names: set[str] = set()

        # С небольшим запасом по датам, чтобы не откусить края
        day = window_start.date()
        last_day = window_end.date()
        margin_sec = 120  # запас по секундам при отборе интересов по окну

        while day <= last_day:
            day_str = day.strftime("%Y.%m.%d")
            cloud_names = cloud_uploader._list_cloud_interest_folders_for_day(client, plate, day_str)
            for name in cloud_names:
                try:
                    _, s_dt, e_dt = main_funcs._interest_name_to_interval(name)
                except Exception:
                    continue
                # отсекаем интересы, которые явно вне окна recheck, с запасом
                if e_dt < (window_start - datetime.timedelta(seconds=margin_sec)):
                    continue
                if s_dt > (window_end + datetime.timedelta(seconds=margin_sec)):
                    continue
                expected_names.add(name)
            day += datetime.timedelta(days=1)

        detected_names: set[str] = set()
        for it in recheck_interests:
            nm = (it or {}).get("name")
            if nm:
                detected_names.add(nm)

        new_exact, missing_exact = main_funcs.exact_diff_sets(expected_names, detected_names)

        # 2. Новые интересы → в pending_interests
        if new_exact:
            raw_to_append = [it for it in recheck_interests if it.get("name") in new_exact]
            to_append: list[dict] = []
            for it in raw_to_append:
                name = it.get("name")
                if not name:
                    continue
                if await self._interest_exists_in_cloud(name):
                    logger.info(f"{reg_id}: RECHECK: {name} уже есть в облаке, пропускаем добавление в pending.")
                    continue
                to_append.append(it)
            if to_append:
                logger.info(
                    f"{reg_id}: RECHECK: обнаружено {len(to_append)} новых интересов по сравнению с WebDAV "
                    f"(будут добавлены в pending_interests)."
                )
                main_funcs.append_pending_interests(reg_id, to_append)

        # 3. Устаревшие интересы → удалить с облака
        for name in sorted(missing_exact):
            try:
                plate2, s_dt, _ = main_funcs._interest_name_to_interval(name)
            except Exception:
                logger.warning(f"{reg_id}: RECHECK: не удалось распарсить имя устаревшего интереса '{name}' — пропуск.")
                continue

            day_str = s_dt.strftime("%Y.%m.%d")
            folder_path = f"{settings.CLOUD_PATH}/{plate2}/{day_str}/{name}"

            try:
                logger.info(f"{reg_id}: RECHECK: удаляем устаревший интерес с WebDAV: {folder_path}")
                client.clean(folder_path)
            except cloud_uploader.RemoteResourceNotFound:
                logger.info(f"{reg_id}: RECHECK: папка интереса уже отсутствует: {folder_path}")
            except Exception as e:
                logger.error(f"{reg_id}: RECHECK: ошибка при удалении интереса '{name}' из WebDAV: {e}")


    async def _interest_exists_in_cloud(self, interest_name: str) -> bool:
        """
        Осторожно проверяет наличие папки интереса в облаке с кэшем WebDAV.
        """
        try:
            _, _, folder_path = cloud_uploader.get_interest_folder_path(interest_name, settings.CLOUD_PATH)
        except Exception as e:
            logger.warning(f"Не удалось получить путь интереса '{interest_name}' для проверки на облаке: {e}")
            return False

        try:
            return await cloud_uploader.cached_check(cloud_uploader.client, folder_path)
        except Exception as e:
            logger.warning(f"[WEBDAV] Ошибка cached_check для '{folder_path}': {e}")
            return False


    async def get_devices_online(self):
        try:
            devices = await cms_gate_client.list_devices(status="online", timeout_sec=180.0)
            if devices:
                logger.debug(f"Got devices online from cms_gate: {devices}")
            else:
                logger.debug("No devices online from cms_gate.")
            self._last_devices_online = devices
            logger.info(
                pipeline_event(
                    "devices_online_poll",
                    count=len(devices or []),
                    fallback=False,
                )
            )
            return devices
        except TimeoutError as e:
            fallback_count = len(self._last_devices_online or [])
            logger.warning(
                f"list_devices timeout in cms_gate: {e}. "
                f"Using last known online devices ({fallback_count})."
            )
            logger.warning(
                pipeline_event(
                    "devices_online_poll",
                    count=len(self._last_devices_online or []),
                    fallback=True,
                )
            )
            return self._last_devices_online or []

    async def operate_device(self, reg_id, plate):
        if reg_id in self.devices_in_progress:
            return
        self.devices_in_progress.append(reg_id)
        try:
            await self.download_reg_videos(reg_id, plate)
        except Exception:
            logger.error(traceback.format_exc())
        finally:
            # гарантированно освобождаем
            if reg_id in self.devices_in_progress:
                self.devices_in_progress.remove(reg_id)

    async def get_interests_async(self, reg_id, reg_info, start_time, stop_time):
        """
        Асинхронная версия получения интересов:
        - CMS треки (queryTrackDetail) — в thread-пуле через asyncio.to_thread
        - CMS alarm detail — в thread-пуле через asyncio.to_thread
        - Подготовка алармов/сшивка — синхронно (CPU), можно оставить в основном потоке
        Логика «шага назад по минуте» (max_extra_pulls) сохранена.
        """
        max_extra_pulls = 8  # максимум шагов назад по минуте
        pulls = 0

        while True:
            start_time_dt = datetime.datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
            tracks_prealarm_lookback_sec = settings.config.getint(
                "Interests", "TRACKS_PREALARM_LOOKBACK_SEC", fallback=120
            )
            query_start_time = (start_time_dt - datetime.timedelta(seconds=tracks_prealarm_lookback_sec)).strftime(
                self.TIME_FMT
            )

            # Берём треки и алармы через cms_gate (прямые HTTP)
            tracks, all_alarms = await cms_gate_client.get_tracks_and_alarms(
                reg_id=reg_id,
                start_time=query_start_time,
                end_time=stop_time,
            )
            #for alarm in all_alarms:
            #    print(alarm)

            prepared = cms_api_funcs.prepare_alarms(
                raw_alarms=all_alarms,
                reg_cfg=reg_info,
                allowed_atp=frozenset({19, 20, 21, 22}),
                min_stop_speed_kmh=settings.config.getint("Interests", "MIN_STOP_SPEED") / 10.0,
                merge_gap_sec=15,
                reg_id=reg_id
            )

            try:
                interests = cms_api_funcs.find_interests_by_lifting_switches(
                    tracks=tracks,
                    start_tracks_search_time=start_time_dt,
                    reg_id=reg_id,
                    alarms=prepared,
                )
            except cms_api_funcs.LoadingInProgress:
                logger.info("Прерываем обработку интересов потому что машина грузится в это время ")
                return {"error": "Loading in progress"}

            if isinstance(interests, dict) and "interests" in interests:
                found_interests = interests["interests"]
                
                # Fallback: если интересов не найдено, но есть алармы, попробуем создать интересы напрямую из алармов
                if not found_interests and prepared and isinstance(prepared, dict) and "alarms" in prepared:
                    logger.info(f"{reg_id}: [FALLBACK] Интересы не найдены основным методом, пробуем поиск напрямую по алармам")
                    fallback_interests = []
                    for alarm in prepared["alarms"]:
                        # Берем только алармы с известным типом груза и start_stopped=True
                        if alarm.get("start_stopped") and alarm.get("cargo_type") != "unknown":
                            start_dt = alarm.get("start_dt")
                            end_dt = alarm.get("end_dt")
                            cargo_type = alarm.get("cargo_type")
                            
                            if start_dt and end_dt:
                                # Создаем простой интерес из аларма
                                time_before = (start_dt - datetime.timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
                                time_after = (end_dt + datetime.timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
                                
                                # Используем последний трек для создания интереса, если есть треки
                                if tracks:
                                    last_track = tracks[-1]
                                    interest = cms_api_funcs.get_interest_from_track(
                                        last_track,
                                        start_time=time_before,
                                        end_time=time_after,
                                        photo_before_timestamp=time_before,
                                        photo_after_timestamp=time_after,
                                        reg_id=reg_id,
                                    )
                                    interest["report"] = {
                                        "cargo_type": "Бункер" if cargo_type == "kgo" else "Контейнер",
                                        "geo": last_track.get("ps"),
                                        "switches_amount": 1,
                                        "switch_events": [{
                                            "datetime": alarm.get("start_str"),
                                            "switch": alarm.get("io_index"),
                                            "source": "alarm-fallback"
                                        }],
                                    }
                                    fallback_interests.append(interest)
                                    logger.info(f"{reg_id}: [FALLBACK] Создан интерес из аларма {alarm.get('start_str')}: {time_before} → {time_after}")
                    
                    if fallback_interests:
                        logger.info(f"{reg_id}: [FALLBACK] Найдено {len(fallback_interests)} интересов через fallback по алармам")
                        return fallback_interests
                
                return found_interests

            elif isinstance(interests, dict) and "error" in interests:
                pulls += 1
                if pulls > max_extra_pulls:
                    logger.warning(f"[GUARD] Достигнут предел догрузок (pulls={pulls}). Останавливаемся.")
                    return []
                # двигаемся на минуту назад
                start_time = (start_time_dt - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"Теперь ищем треки с {start_time}")
                continue

            else:
                # На случай иных форматов ответа
                logger.warning(f"[ANALYZE] Неожиданный формат из find_interests_by_lifting_switches: {type(interests)}")
                return []

    def _parse_start_ts(self, it: dict):
        try:
            return datetime.datetime.strptime(it.get("start_time", ""), settings.TIME_FMT)
        except Exception:
            return datetime.datetime.max  # если испорченный интерес — обрабатываем в самом конце

    def _log_interests_shape(self, reg_id: str, stage: str, interests):
        sample_type = "n/a"
        sample_repr = "n/a"
        if isinstance(interests, list):
            if interests:
                sample_type = type(interests[0]).__name__
                sample_repr = repr(interests[0])[:600]
        else:
            sample_type = type(interests).__name__
            sample_repr = repr(interests)[:600]
        try:
            size = len(interests)
        except Exception:
            size = "n/a"
        logger.info(
            f"{reg_id}: [{stage}] interests_shape type={type(interests).__name__} "
            f"size={size} sample_type={sample_type} sample={sample_repr}"
        )

    def _merge_interests_safe(self, reg_id: str, stage: str, raw) -> list[dict] | None:
        """
        Приводит ответ get_interests_async к списку и вызывает merge_overlapping_interests.

        None — прервать только refill: CMS вернул «Loading in progress» (нельзя вызывать merge по dict).
        Пустой список — интересов нет.
        """
        if isinstance(raw, dict) and raw.get("error") == "Loading in progress":
            logger.info(f"{reg_id}: [{stage}] пропуск merge: машина грузится (Loading in progress)")
            return None
        if raw is None or raw == []:
            return []
        if isinstance(raw, dict):
            if "error" in raw:
                logger.warning(f"{reg_id}: [{stage}] ответ с полем error: {raw!r}")
                return []
            logger.warning(f"{reg_id}: [{stage}] неожиданный dict вместо списка интересов: {raw!r}")
            return []
        if not isinstance(raw, list):
            logger.warning(f"{reg_id}: [{stage}] неожиданный тип {type(raw).__name__}")
            return []
        cleaned = [x for x in raw if isinstance(x, dict)]
        if len(cleaned) != len(raw):
            logger.warning(f"{reg_id}: [{stage}] отфильтрованы не-dict элементы в списке интересов")
        if not cleaned:
            return []
        return merge_overlapping_interests(cleaned)

    async def download_reg_videos(self, reg_id, plate):
        logger.debug(f"{reg_id}. Начинаем работу с устройством.")

        # Информация о регистраторе
        reg_info = main_funcs.get_reg_info(reg_id) or main_funcs.create_new_reg(reg_id, plate)
        logger.debug(f"{reg_id}. Информация о регистраторе: {reg_info}.")

        ignore = reg_info.get("ignore", False)
        if ignore:
            logger.debug(f"{reg_id}. Игнорируем регистратор, поскольку в states.json параметр ignore=true.")
            return

        logger.info(
            pipeline_event(
                "device_run_start",
                reg_id=str(reg_id),
                plate=str(plate or ""),
            )
        )

        # Pending - уже извлеченные из CMS и сохраненные в states.json интересы
        pending = main_funcs.get_pending_interests(reg_id)
        if not pending:
            await self._refill_pending_interests_if_due(reg_id)     # Извлечь новые интересы из CMS
            pending = main_funcs.get_pending_interests(reg_id)

        if pending:
            interests = pending
            logger.info(f"{reg_id}: Берём {len(interests)} интерес(а/ов) из очереди pending_interests.")
            logger.debug(f"({interests})")
        else:
            # Если очередь пуста и проверка давности не прошла — делать лишних запросов не будем
            logger.info(f"{reg_id}: Очередь pending_interests пуста, и наполнять сейчас рано — завершаем.")
            return True

        logger.info(f"{reg_id}: Найдено {len(interests)} интересов")
        self._log_interests_shape(reg_id, "DOWNLOAD_REG_BEFORE_MERGE", interests)
        merged = self._merge_interests_safe(reg_id, "DOWNLOAD_REG_BEFORE_MERGE", interests)
        if merged is None:
            logger.warning(
                f"{reg_id}: pending дал Loading in progress — пропускаем обход (повторим позже)."
            )
            return True
        if not merged:
            logger.warning(f"{reg_id}: после merge список интересов пуст — завершаем обход.")
            return True
        interests = merged
        logger.info(f"{reg_id}: К запуску {len(interests)} интересов (после фильтра processed).")

        # сортируем интересы по времени начала, старые сначала
        interests.sort(key=self._parse_start_ts)

        total_found = len(interests)
        max_per_batch = settings.config.getint("Interests", "MAX_INTERESTS_PER_BATCH", fallback=8)
        if total_found > max_per_batch:
            logger.info(
                f"{reg_id}: Берём в работу только {max_per_batch} из {total_found} интересов (батч). "
                f"Остальные — в следующий цикл."
            )
            interests = interests[:max_per_batch]
        else:
            logger.info(f"{reg_id}: Влезают все интересы ({total_found}) в одну пачку.")

        logger.info(
            pipeline_event(
                "batch_scheduled",
                reg_id=str(reg_id),
                interests=len(interests),
                batch_max=max_per_batch,
            )
        )

        # Стартуем задачи (сами ограничители внутри)
        channel_id = reg_info.get("chanel_id")
        tasks = [asyncio.create_task(self._process_one_interest(it, channel_id)) for it in interests]

        # Собираем результаты по мере готовности
        end_times: list[str] = []
        try:
            for coro in asyncio.as_completed(tasks):
                try:
                    et = await coro
                    if et:
                        end_times.append(et)
                except Exception:
                    logger.error(f"{reg_id}: Ошибка в задаче интереса:\n{traceback.format_exc()}")
        finally:
            # на всякий случай — чтобы не остались висячие задачи
            for t in tasks:
                if not t.done():
                    t.cancel()
        logger.info(f"{reg_id}: Пакет интересов завершён: {len(end_times)}/{len(interests)}")
        logger.info(
            pipeline_event(
                "batch_complete",
                reg_id=str(reg_id),
                tasks=len(interests),
                completed_with_end_time=len(end_times),
            )
        )


    async def _process_one_interest(self, interest: dict, channel_id) -> str | None:
        reg_id = interest.get("reg_id")
        # Ограничители глобально и по устройству
        async with self._get_global_sem(), self._get_device_sem(reg_id):
            created_start_time = datetime.datetime.now()
            interest_name = interest["name"]

            logger.info(f"{reg_id}: Начинаем работу с интересом {interest_name}")
            logger.debug(f"{interest}")

            # Создаём пути в облаке под интерес
            cloud_paths = await cloud_uploader.create_interest_folder_path_async(
                name=interest_name,
                dest=settings.CLOUD_PATH
            )

            if not cloud_paths:
                logger.error(f"{reg_id}: Не удалось создать папки для {interest_name}. Пропускаем интерес.")
                logger.info(
                    pipeline_event(
                        "interest_outcome",
                        reg_id=str(reg_id),
                        interest=str(interest_name),
                        outcome="fail_no_cloud",
                    )
                )
                self.del_pending_interest(reg_id, interest)
                return interest["end_time"]

            interest_cloud_folder = cloud_paths["interest_folder_path"]
            interest["cloud_folder"] = interest_cloud_folder
            pics_after_folder = posixpath.join(interest_cloud_folder, "after_pics")
            pics_before_folder = posixpath.join(interest_cloud_folder, "before_pics")
            interest["pics_before_folder"] = pics_before_folder
            interest["pics_after_folder"] = pics_after_folder
            await cloud_uploader.acreate_folder_if_not_exists(cloud_uploader.client, pics_before_folder)
            await cloud_uploader.acreate_folder_if_not_exists(cloud_uploader.client, pics_after_folder)

            # 1) проверяем наличие полного видео интереса в облаке
            interest_video_exists = await cloud_uploader.check_if_interest_video_exists(interest_name)

            # 2) какие каналы нужны для кадров
            before_channels_to_download, after_channels_to_download = await self.get_channels_to_download_pics(
                interest_cloud_folder
            )

            # 3) если видео по интересу в облаке НЕТ — добавляем канал полного ролика
            to_download_for_full_clip = [channel_id] if not interest_video_exists else []
            logger.debug(f"BEFORE,AFTER,FULL: {before_channels_to_download}, {after_channels_to_download}, {to_download_for_full_clip}")
            # детерминированное объединение без дублей
            final_channels_to_download = sorted({
                *before_channels_to_download,
                *after_channels_to_download,
                *to_download_for_full_clip
            })

            logger.debug(
                f"{reg_id}. {interest_name} Нужно скачать видео интереса: {not interest_video_exists}. "
                f"Кадры ДО: {before_channels_to_download}. "
                f"Кадры ПОСЛЕ: {after_channels_to_download}. "
                f"Итого каналы: {final_channels_to_download}"
            )

            if not final_channels_to_download:
                logger.info("Нечего скачивать, все материалы уже есть в облаке.")
                logger.info(
                    pipeline_event(
                        "interest_outcome",
                        reg_id=str(reg_id),
                        interest=str(interest_name),
                        outcome="skipped_already_in_cloud",
                    )
                )
                self.del_pending_interest(reg_id, interest)
                return None

            # 4) скачиваем по одному клипу на канал через cms_gate (ретраи HTTP в клиенте + раунды при дырках)
            def _all_requested_paths_present(m: dict) -> bool:
                for ch in final_channels_to_download:
                    info = m.get(ch) or m.get(str(ch))
                    if not (info or {}).get("path"):
                        return False
                return True

            download_rounds = max(1, settings.config.getint("Interests", "DOWNLOAD_INTEREST_ROUNDS", fallback=3))
            round_delay = float(
                settings.config.get("Interests", "DOWNLOAD_INTEREST_ROUND_DELAY_SEC", fallback="20")
            )
            channels_files_dict: dict = {}
            t_dl = time.perf_counter()
            for round_i in range(1, download_rounds + 1):
                channels_files_dict = await cms_gate_client.download_clips_for_interest(
                    reg_id=reg_id,
                    interest=interest,
                    channels=final_channels_to_download,
                )
                if _all_requested_paths_present(channels_files_dict):
                    break
                if round_i < download_rounds:
                    missing = [
                        ch
                        for ch in final_channels_to_download
                        if not (channels_files_dict.get(ch) or channels_files_dict.get(str(ch)) or {}).get(
                            "path"
                        )
                    ]
                    logger.warning(
                        f"{reg_id}: {interest_name} раунд {round_i}/{download_rounds}: нет path по каналам "
                        f"{missing} — повтор через {round_delay * round_i:.0f}s"
                    )
                    await asyncio.sleep(min(120.0, round_delay * round_i))
            elapsed_dl = time.perf_counter() - t_dl
            missing_after = [
                ch
                for ch in final_channels_to_download
                if not (channels_files_dict.get(ch) or channels_files_dict.get(str(ch)) or {}).get("path")
            ]
            logger.info(
                pipeline_event(
                    "interest_download",
                    reg_id=str(reg_id),
                    interest=str(interest_name),
                    elapsed_sec=elapsed_dl,
                    rounds_max=download_rounds,
                    paths_complete=len(missing_after) == 0,
                    missing_count=len(missing_after),
                )
            )
            # оставляем полную структуру для доступа к concat_sources при отладке
            channels_info = channels_files_dict

            channels_paths = {ch: info["path"] for ch, info in channels_info.items() if info and info.get("path")}
            # 5) если надо — выгружаем «полный» клип в облако (только для chanel_id)
            full_clip_upload_status = False
            full_clip_path = None

            if not interest_video_exists:
                _ch_full = int(channel_id) if channel_id is not None else None
                file_dict = None
                if _ch_full is not None:
                    file_dict = channels_files_dict.get(_ch_full) or channels_files_dict.get(str(_ch_full))
                if file_dict:
                    full_clip_path = file_dict.get("path")
                else:
                    full_clip_path = None
                if full_clip_path:
                     full_clip_upload_status = await self.upload_interest_video_cloud(
                        reg_id=reg_id,
                        interest_name=interest_name,
                        video_path=full_clip_path,
                        cloud_folder=cloud_paths["interest_folder_path"]
                    )
                else:
                    logger.warning(
                        f"{reg_id}: Полный клип по каналу {channel_id} не получен — пропускаем загрузку видео.")

            await cloud_uploader.aupload_dict_as_json_to_cloud(
                data=interest["report"],
                remote_folder_path=interest["cloud_folder"]
            )

            # 6) извлекаем кадры из КАЖДОГО скачанного клипа и выгружаем их
            upload_status = await self.process_frames_before_after(
                reg_id, interest, channels_paths  # ← передаём словарь!!!
            )
            ok_frames = bool(upload_status and upload_status.get("upload_status"))
            logger.info(f"Результат загрузки изображений: {ok_frames}")

            # 7) чистим локальные клипы (кроме «полного» по нужному каналу)
            removed = video_utils.delete_videos_except(
                videos_by_channel=channels_paths,
                keep_channel_id=channel_id if not interest_video_exists else None,
            )
            need_full_video = not interest_video_exists
            video_ok = interest_video_exists or bool(full_clip_upload_status)
            all_done_ok = bool(ok_frames) and (not need_full_video or video_ok)

            if full_clip_path:
                if full_clip_upload_status:
                    logger.info(
                        f"{reg_id}: Удаляем локальное видео интереса {interest_name}. ({full_clip_path}).")
                    if os.path.exists(full_clip_path):
                        os.remove(full_clip_path)
                else:
                    logger.error(f"{reg_id}: Не удалось загрузить видео интереса в {interest_name}.")
            if all_done_ok:
                if settings.config.getboolean("QT_RM", "enable_recognition"):
                    logger.info(f"{reg_id}: {interest_name} Отдаем команду на распознавание (выстерлил-забыл)")
                    asyncio.create_task(
                        self.qt_rm_client.recognize_webdav(interest_name=interest_name)
                    )
                self.del_pending_interest(reg_id, interest)
                total_src_removed = 0
                for ch, info in channels_info.items():
                    sources = (info or {}).get("concat_sources") or []
                    for fp in sources:
                        try:
                            if os.path.exists(fp):
                                os.remove(fp)
                                total_src_removed += 1
                        except Exception as e:
                            logger.warning(f"{reg_id}: Не удалось удалить исходник {fp}: {e}")
                interest_temp_folder = os.path.join(settings.TEMP_FOLDER,
                                                    interest_name)
                if os.path.exists(interest_temp_folder):
                    logger.info(
                        f"{reg_id}: Удаляем временную директорию интереса {interest_name}. ({interest_temp_folder}).")
                    shutil.rmtree(interest_temp_folder)

            logger.info(f"{reg_id}: V2 завершено. Upload={upload_status}. Удалено видеофайлов: {removed}.")
            qt_sent = bool(
                all_done_ok
                and settings.config.getboolean("QT_RM", "enable_recognition")
            )
            logger.info(
                pipeline_event(
                    "interest_outcome",
                    reg_id=str(reg_id),
                    interest=str(interest_name),
                    outcome="success" if all_done_ok else "partial",
                    all_done_ok=all_done_ok,
                    video_ok=video_ok,
                    frames_ok=ok_frames,
                    pending_cleared=all_done_ok,
                    qt_recognition_scheduled=qt_sent,
                )
            )

        await cloud_uploader.append_report_line_to_cloud_async(
            remote_folder_path=cloud_paths["date_folder_path"],
            created_start_time=created_start_time.strftime(self.TIME_FMT),
            created_end_time=datetime.datetime.now().strftime(self.TIME_FMT),
            file_name=interest_name
        )

        # Маркируем интерес как обработанный (локально)
        #main_funcs._save_processed(reg_id, interest_name)

        return interest["end_time"]

    def del_pending_interest(self, reg_id, interest_or_name):
        names_to_remove = []
        if isinstance(interest_or_name, dict):
            merged_name = interest_or_name.get("name")
            if isinstance(merged_name, str) and merged_name:
                names_to_remove.append(merged_name)
            source_names = interest_or_name.get("_source_names") or []
            if isinstance(source_names, list):
                for nm in source_names:
                    if isinstance(nm, str) and nm:
                        names_to_remove.append(nm)
        elif isinstance(interest_or_name, str) and interest_or_name:
            names_to_remove.append(interest_or_name)

        # Стабильный порядок + дедуп
        unique_names = []
        seen = set()
        for nm in names_to_remove:
            if nm not in seen:
                unique_names.append(nm)
                seen.add(nm)

        if not unique_names:
            logger.warning(f"{reg_id}: del_pending_interest вызван без валидного имени интереса: {interest_or_name!r}")
            return

        for nm in unique_names:
            logger.info(f"{reg_id}: Удаляем pending interest: {nm}")
            try:
                main_funcs.remove_pending_interest(reg_id, nm)
            except Exception as e:
                logger.warning(f"{reg_id}: Не удалось удалить {nm} из pending_interests: {e}")


    async def get_channels_to_download_pics(self, interest_cloud_path):
        pics_after_folder = posixpath.join(interest_cloud_path, "after_pics")
        pics_before_folder = posixpath.join(interest_cloud_path, "before_pics")

        channels = [0, 1, 2, 3]

        # Параллельные проверки наличия на облаке
        before_checks = [asyncio.create_task(cloud_uploader._frame_exists_cloud_async(pics_before_folder, ch)) for ch in channels]
        after_checks = [asyncio.create_task(cloud_uploader._frame_exists_cloud_async(pics_after_folder, ch)) for ch in channels]

        before_exists = await asyncio.gather(*before_checks)
        after_exists = await asyncio.gather(*after_checks)
        before_channels_to_download = [ch for ch, exists in zip(channels, before_exists) if not exists]
        after_channels_to_download = [ch for ch, exists in zip(channels, after_exists) if not exists]
        return before_channels_to_download, after_channels_to_download

    async def process_frames_before_after(self, reg_id: str, enriched: dict, videos_by_channel):
        """
        ВЕРСИЯ 3 (streaming):
        1) Из каждого клипа берём первый и последний кадр как JPEG bytes (без локальных файлов)
        2) Заливаем в облако в before_pics / after_pics через PUT
        Возвращает: {"upload_status": bool}
        """
        channels = [0, 1, 2, 3]

        before_items: list[tuple[str, bytes]] = []
        after_items: list[tuple[str, bytes]] = []

        async def _extract_for_channel(ch: int, path: str | None):
            if not path:
                return None, None
            return await video_utils.extract_edge_frames_bytes(
                video_path=path,
                channel_id=ch,
                reg_id=reg_id,
            )

        # Создаём словарь task -> channel для корректного маппинга
        task_to_channel = {}
        for ch in channels:
            task = asyncio.create_task(_extract_for_channel(ch, videos_by_channel.get(ch)))
            task_to_channel[task] = ch

        # Собираем результаты по мере готовности
        for task in asyncio.as_completed(task_to_channel.keys()):
            first_item, last_item = await task
            if first_item:
                before_items.append(first_item)
            if last_item:
                after_items.append(last_item)

        # Загрузка без временных файлов
        ok_before = await cloud_uploader.upload_many_bytes_async(before_items, enriched["pics_before_folder"],
                                                                 content_type="image/jpeg")
        ok_after = await cloud_uploader.upload_many_bytes_async(after_items, enriched["pics_after_folder"],
                                                                content_type="image/jpeg")
        upload_status = bool(ok_before and ok_after)

        return {"upload_status": upload_status}


    async def upload_interest_video_cloud(self, reg_id, interest_name, video_path, cloud_folder):
        # Загружаем видео
        logger.info(
            f"{reg_id}: Загружаем видео интереса {interest_name} в облако.")
        upload_status = await asyncio.to_thread(
            cloud_uploader.upload_file, video_path, cloud_folder)
        return upload_status

    async def login(self):
        """
        Авторизация в CMS больше не требуется напрямую, так как все обращения
        к CMS идут через внешний сервис cms_gate.
        """
        self.jsession = None

    async def mainloop(self):
        logger.info("Mainloop has been launched with success.")
        self._running: set[asyncio.Task] = set()
        await self.login()

        while True:
            # важно: get_devices_online в thread, чтобы не блокировать loop
            devices_online = await self.get_devices_online()

            for device_dict in devices_online:
                reg_id = device_dict["did"]
                plate = device_dict["vid"]

                # если девайс уже в работе — пропускаем
                if reg_id in self.devices_in_progress:
                    continue

                async def _run_with_limit(rid, pl):
                    async with self._get_devices_sem():
                        await self.operate_device(rid, pl)

                # Стартуем корутину и НЕ ждём всю пачку
                t = asyncio.create_task(_run_with_limit(reg_id, plate))
                self._running.add(t)
                t.add_done_callback(self._running.discard)

            await asyncio.sleep(120)

    async def _refill_pending_interests_if_due(self, reg_id: str) -> None:
        """
        Пополняет очередь pending_interests для reg_id двумя способами:

        1) "Обычный" forward-проход:
           - если сейчас > last_upload_time + 600 сек:
             - догоняем интервал [last_upload_time → now] посуточно,
               обновляя last_upload_time по мере продвижения.

        2) Recheck-проход по "верифицированному" интервалу:
           - берём verified_until (если его нет — используем last_upload_time),
           - если прошло >= VERIFIED_RECHECK_HOURS часов,
             ещё раз проверяем интервал [verified_until → now],
             СРАВНИВАЕМ с WebDAV и:
               * новые интересы -> в pending_interests
               * старые (больше не найденные) -> удаляем с облака.

        Ограничение глубины по дням (MAX_LOOKBACK_DAYS) применяется и к last_upload_time,
        и к verified_until, чтобы не уходить слишком далеко в прошлое.
        """
        if reg_id in self._interest_refill_in_progress:
            return
        self._interest_refill_in_progress.add(reg_id)
        try:
            TIME_FMT = "%Y-%m-%d %H:%M:%S"

            reg_info = main_funcs.get_reg_info(reg_id)

            # --- last_upload_time (как раньше) ---
            last_up_str = reg_info.get("last_upload_time")
            if not last_up_str:
                # если ничего нет — считаем 7 дней назад
                last_up_str = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime(TIME_FMT)
            last_up = datetime.datetime.strptime(last_up_str, TIME_FMT)

            now = datetime.datetime.now()

            # --- verified_until ---
            verified_str = reg_info.get("verified_until") or last_up_str
            try:
                verified_dt = datetime.datetime.strptime(verified_str, TIME_FMT)
            except Exception:
                logger.warning(
                    f"{reg_id}: Некорректный verified_until='{verified_str}', "
                    f"сбрасываем к last_upload_time={last_up_str}."
                )
                verified_dt = last_up

            verified_long_str = reg_info.get("verified_until_long") or verified_str
            try:
                verified_long_dt = datetime.datetime.strptime(verified_long_str, TIME_FMT)
            except Exception:
                logger.warning(
                    f"{reg_id}: Некорректный verified_until_long='{verified_long_str}', "
                    f"сбрасываем к verified_until={verified_str}."
                )
                verified_long_dt = verified_dt

            # --- флаги "пора ли что-то делать" ---
            forward_due = (now - last_up).total_seconds() >= 600

            recheck_hours = settings.config.getint("Interests", "VERIFIED_RECHECK_HOURS", fallback=6)
            recheck_due = False
            if recheck_hours > 0:
                recheck_due = (now - verified_dt).total_seconds() >= recheck_hours * 3600

            recheck_long_hours = settings.config.getint("Interests", "VERIFIED_RECHECK_HOURS_LONG", fallback=24)
            long_recheck_due = False
            if recheck_long_hours > 0:
                long_recheck_due = (now - verified_long_dt).total_seconds() >= recheck_long_hours * 3600

            if not forward_due and not recheck_due and not long_recheck_due:
                return

            # --- ограничение глубины по дням ---
            max_lookback_days = settings.config.getint("Interests", "MAX_LOOKBACK_DAYS", fallback=0)
            if max_lookback_days > 0:
                earliest_allowed = now - datetime.timedelta(days=max_lookback_days)

                if last_up < earliest_allowed:
                    logger.info(
                        f"{reg_id}: last_upload_time={last_up.strftime(TIME_FMT)} старее окна "
                        f"{max_lookback_days}d → берём не глубже {earliest_allowed.strftime(TIME_FMT)}."
                    )
                    last_up = earliest_allowed

                if verified_dt < earliest_allowed:
                    logger.info(
                        f"{reg_id}: verified_until={verified_dt.strftime(TIME_FMT)} старее окна "
                        f"{max_lookback_days}d → подрезаем до {earliest_allowed.strftime(TIME_FMT)}."
                    )
                    verified_dt = earliest_allowed

                if verified_long_dt < earliest_allowed:
                    logger.info(
                        f"{reg_id}: verified_until_long={verified_long_dt.strftime(TIME_FMT)} старее окна "
                        f"{max_lookback_days}d → подрезаем до {earliest_allowed.strftime(TIME_FMT)}."
                    )
                    verified_long_dt = earliest_allowed

            collected: list[dict] = []

            def day_end(dt: datetime.datetime) -> datetime.datetime:
                return dt.replace(hour=23, minute=59, second=59)

            def day_start(dt: datetime.datetime) -> datetime.datetime:
                return dt.replace(hour=0, minute=0, second=0)

            # --- 1) Обычный forward-проход от last_upload_time к now ---
            if forward_due:
                cur = last_up
                today = now.date()

                # догоняем до "вчера включительно" посуточно
                while cur.date() < today:
                    st = cur.strftime(TIME_FMT)
                    en_dt = day_end(cur)
                    en = en_dt.strftime(TIME_FMT)

                    reg_cfg = main_funcs.get_reg_info(reg_id) or main_funcs.create_new_reg(reg_id, plate=None)
                    interests = await self.get_interests_async(reg_id, reg_cfg, st, en)
                    if interests:
                        self._log_interests_shape(reg_id, "FORWARD_DAY_BEFORE_MERGE", interests)
                        merged = self._merge_interests_safe(reg_id, "FORWARD_DAY_BEFORE_MERGE", interests)
                        if merged is None:
                            return
                        if merged:
                            collected.extend(merged)
                            en = max(interest["end_time"] for interest in merged)

                    main_funcs.save_new_reg_last_upload_time(reg_id, en)
                    cur = en_dt + datetime.timedelta(seconds=1)

                # остаток "сегодня до текущего момента"
                if cur <= now:
                    st = cur.strftime(TIME_FMT)
                    en = now.strftime(TIME_FMT)
                    reg_cfg = main_funcs.get_reg_info(reg_id) or main_funcs.create_new_reg(reg_id, plate=None)
                    interests = await self.get_interests_async(reg_id, reg_cfg, st, en)
                    if interests:
                        self._log_interests_shape(reg_id, "FORWARD_TODAY_BEFORE_MERGE", interests)
                        merged = self._merge_interests_safe(reg_id, "FORWARD_TODAY_BEFORE_MERGE", interests)
                        if merged is None:
                            return
                        if merged:
                            collected.extend(merged)
                            en = max(interest["end_time"] for interest in merged)
                    main_funcs.save_new_reg_last_upload_time(reg_id, en)

            # --- 2) Recheck-проход от verified_until к now ---
            if recheck_due:
                st = verified_dt.strftime(TIME_FMT)
                en = now.strftime(TIME_FMT)

                logger.info(
                    f"{reg_id}: RECHECK интервала [{st} → {en}] "
                    f"после паузы {recheck_hours}ч."
                )

                reg_cfg = main_funcs.get_reg_info(reg_id) or main_funcs.create_new_reg(reg_id, plate=None)
                recheck_interests = await self.get_interests_async(reg_id, reg_cfg, st, en)
                if recheck_interests:
                    self._log_interests_shape(reg_id, "RECHECK_BEFORE_MERGE", recheck_interests)
                    merged_recheck = self._merge_interests_safe(reg_id, "RECHECK_BEFORE_MERGE", recheck_interests)
                    if merged_recheck is None:
                        return
                    if merged_recheck:
                        # ВАЖНО: не добавляем их в collected, а синхронизируем с облаком
                        await self._sync_recheck_with_cloud(
                            reg_id=reg_id,
                            recheck_interests=merged_recheck,
                            st=st,
                            en=en,
                            time_fmt=TIME_FMT,
                        )

                # фиксируем, что этот интервал проверен
                main_funcs.save_reg_verified_until(reg_id, en)
                try:
                    verified_dt = datetime.datetime.strptime(en, TIME_FMT)
                except Exception:
                    pass

            if long_recheck_due:
                st_long = verified_long_dt.strftime(TIME_FMT)
                en_long = verified_dt.strftime(TIME_FMT)

                if st_long != en_long:
                    logger.info(
                        f"{reg_id}: DAILY RECHECK интервала [{st_long} → {en_long}] "
                        f"после паузы {recheck_long_hours}ч."
                    )

                    reg_cfg = main_funcs.get_reg_info(reg_id) or main_funcs.create_new_reg(reg_id, plate=None)
                    long_recheck_interests = await self.get_interests_async(reg_id, reg_cfg, st_long, en_long)
                    if long_recheck_interests:
                        self._log_interests_shape(reg_id, "LONG_RECHECK_BEFORE_MERGE", long_recheck_interests)
                        merged_long = self._merge_interests_safe(
                            reg_id, "LONG_RECHECK_BEFORE_MERGE", long_recheck_interests
                        )
                        if merged_long is None:
                            return
                        if merged_long:
                            await self._sync_recheck_with_cloud(
                                reg_id=reg_id,
                                recheck_interests=merged_long,
                                st=st_long,
                                en=en_long,
                                time_fmt=TIME_FMT,
                            )

                    main_funcs.save_reg_verified_until_long(reg_id, en_long)
                    try:
                        verified_long_dt = datetime.datetime.strptime(en_long, TIME_FMT)
                    except Exception:
                        pass

            # --- forward-результат пишем в pending_interests (с дедупом по имени) ---
            if collected:
                main_funcs.append_pending_interests(reg_id, collected)

        finally:
            self._interest_refill_in_progress.discard(reg_id)



async def _run():
    d = Main()
    try:
        await d.mainloop()
    finally:
        # всегда освобождаем соединения cms_gate_client
        await cms_gate_client.close_client()


if __name__ == "__main__":
    asyncio.run(_run())
