import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType


class _ManualServerInstance:
    """模拟 Emby 模块的 instance，复用现有 __emby_get / __emby_delete 流程"""

    def __init__(self, host: str, apikey: str):
        self._host = host.rstrip("/")
        self._apikey = apikey
        import requests as req
        self._session = req.Session()

    def _build(self, url: str) -> str:
        return url.replace("[HOST]", self._host + "/").replace("[APIKEY]", self._apikey)

    def get_data(self, url: str):
        try:
            return self._session.get(self._build(url), timeout=30)
        except Exception as e:
            logger.warning(f"手动 Emby GET 失败: {e}")
            return None

    def delete_data(self, url: str):
        try:
            return self._session.delete(self._build(url), timeout=30)
        except Exception as e:
            logger.warning(f"手动 Emby DELETE 失败: {e}")
            return None

    def is_inactive(self) -> bool:
        try:
            r = self._session.get(
                f"{self._host}/emby/System/Info?api_key={self._apikey}", timeout=10
            )
            return r.status_code >= 400
        except Exception:
            return True


class _ManualServerConfig:
    def __init__(self, host: str, apikey: str):
        self.config = {"host": host, "apikey": apikey}


class _ManualServiceInfo:
    def __init__(self, name: str, host: str, apikey: str):
        self.name = name
        self.type = "emby"
        self.config = _ManualServerConfig(host, apikey)
        self.instance = _ManualServerInstance(host, apikey)


class EmbyAutoClean(_PluginBase):
    # 插件名称
    plugin_name = "Emby媒体自动清理"
    # 插件描述
    plugin_desc = "定期清理 Emby 服务器上添加时间久远且未被收藏的媒体条目，支持多服务器、Dry-run 预演和逐条通知。"
    # 插件图标
    plugin_icon = "clean.png"
    # 插件版本
    plugin_version = "1.2.1"
    # 插件作者
    plugin_author = "WWWWM03"
    # 作者主页
    author_url = "https://github.com/WWWWM03/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "embyautoclean_"
    # 加载顺序
    plugin_order = 24
    # 可使用的用户级别
    auth_level = 1

    # ───────── 私有属性 ─────────
    _enabled: bool = False
    _onlyonce: bool = False
    _notify: bool = True
    _dry_run: bool = True

    _cron: str = "0 3 * * *"

    _mediaservers: List[str] = []
    _library_names: str = ""
    _media_types: List[str] = ["Movie", "Series"]

    _days_threshold: int = 180
    _max_deletions: int = 50

    _favorite_scope: str = "any_user"
    _favorite_users: str = ""

    _exclude_keywords: str = ""
    _exclude_tags: str = ""
    _skip_if_played: bool = True

    _notify_type: str = "MediaServer"

    _manual_servers: str = ""

    # v1.2.0 收藏诊断模式（不执行任何删除，仅输出每个用户的收藏清单）
    _diagnose_favorites: bool = False

    # 内部
    _scheduler: Optional[BackgroundScheduler] = None
    _HISTORY_MAX: int = 500

    # ───────── 生命周期 ─────────
    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = bool(config.get("enabled", False))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._notify = bool(config.get("notify", True))
            self._dry_run = bool(config.get("dry_run", True))

            self._cron = config.get("cron") or "0 3 * * *"

            self._mediaservers = config.get("mediaservers") or []
            self._library_names = config.get("library_names") or ""
            self._media_types = config.get("media_types") or ["Movie", "Series"]

            try:
                self._days_threshold = int(config.get("days_threshold") or 180)
            except (TypeError, ValueError):
                self._days_threshold = 180
            try:
                self._max_deletions = int(config.get("max_deletions") or 50)
            except (TypeError, ValueError):
                self._max_deletions = 50

            self._favorite_scope = config.get("favorite_scope") or "any_user"
            self._favorite_users = config.get("favorite_users") or ""

            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._exclude_tags = config.get("exclude_tags") or ""
            self._skip_if_played = bool(config.get("skip_if_played", True))

            self._notify_type = config.get("notify_type") or "MediaServer"

            self._manual_servers = config.get("manual_servers") or ""

            self._diagnose_favorites = bool(config.get("diagnose_favorites", False))

        if self._enabled and self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("Emby媒体自动清理 - 立即运行一次")
            self._scheduler.add_job(
                func=self.__run,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="Emby媒体自动清理（一次性）",
            )
            # 关闭一次性开关并保存
            self._onlyonce = False
            self.update_config({
                "enabled": self._enabled,
                "onlyonce": False,
                "notify": self._notify,
                "dry_run": self._dry_run,
                "cron": self._cron,
                "mediaservers": self._mediaservers,
                "library_names": self._library_names,
                "media_types": self._media_types,
                "days_threshold": self._days_threshold,
                "max_deletions": self._max_deletions,
                "favorite_scope": self._favorite_scope,
                "favorite_users": self._favorite_users,
                "exclude_keywords": self._exclude_keywords,
                "exclude_tags": self._exclude_tags,
                "skip_if_played": self._skip_if_played,
                "notify_type": self._notify_type,
                "manual_servers": self._manual_servers,
                "diagnose_favorites": self._diagnose_favorites,
            })

            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            return [{
                "id": "EmbyAutoClean",
                "name": "Emby媒体自动清理定时服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__run,
                "kwargs": {},
            }]
        return []

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出 EmbyAutoClean 插件失败：{e}")

    # ───────── 服务发现 ─────────
    @property
    def service_infos(self) -> Optional[Dict[str, Any]]:
        """
        合并自动发现（MediaServerHelper）与手动配置的 Emby 服务器
        """
        active: Dict[str, Any] = {}

        # 1) 自动：MediaServerHelper 管理的 Emby
        if self._mediaservers:
            helper = MediaServerHelper()
            services = helper.get_services(name_filters=self._mediaservers) or {}
            for name, si in services.items():
                if not helper.is_media_server(service_type="emby", service=si):
                    logger.info(f"EmbyAutoClean 跳过非 Emby 服务器：{name}（type={si.type}）")
                    continue
                try:
                    if si.instance.is_inactive():
                        logger.warning(f"EmbyAutoClean 媒体服务器 {name} 未连接")
                        continue
                except Exception as e:
                    logger.warning(f"EmbyAutoClean 检查 {name} 连接状态失败：{e}")
                    continue
                active[name] = si

        # 2) 手动：文本框解析
        for entry in self.__parse_manual_servers():
            final_name = entry["name"]
            if final_name in active:
                final_name = f"{entry['name']}[手动]"
            wrapper = _ManualServiceInfo(final_name, entry["host"], entry["apikey"])
            try:
                if wrapper.instance.is_inactive():
                    logger.warning(f"手动 Emby 服务器 {final_name} 未连接")
                    continue
            except Exception as e:
                logger.warning(f"手动 Emby 服务器 {final_name} 连接检查失败：{e}")
                continue
            active[final_name] = wrapper

        if not active:
            logger.warning("EmbyAutoClean 没有可用的 Emby 服务器（自动+手动均为空）")
            return None
        return active

    def __parse_manual_servers(self) -> List[dict]:
        """
        解析 _manual_servers 文本（每行 名称|URL|APIKey）
        """
        out = []
        for line in (self._manual_servers or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) != 3:
                logger.warning(f"手动 Emby 配置格式错误（应为 名称|URL|APIKey）：{line}")
                continue
            name, host, apikey = parts
            if not name or not host or not apikey:
                continue
            if not host.startswith(("http://", "https://")):
                logger.warning(f"手动 Emby 配置 URL 缺少协议头：{host}")
                continue
            out.append({"name": name, "host": host.rstrip("/"), "apikey": apikey})
        return out

    # ───────── 主流程 ─────────
    def __run(self):
        """
        主入口：遍历所有 Emby 服务器并执行清理或收藏诊断
        """
        if not self._enabled:
            return
        infos = self.service_infos
        if not infos:
            return

        if self._diagnose_favorites:
            logger.info(
                f"EmbyAutoClean 进入【收藏诊断】模式（不会执行任何删除），"
                f"收藏范围：{self._favorite_scope}，媒体类型：{self._media_types}"
            )
            for server_name, svc in infos.items():
                try:
                    self.__run_diagnose(server_name, svc)
                except Exception as e:
                    logger.error(f"EmbyAutoClean 诊断服务器 {server_name} 时发生异常：{e}", exc_info=True)
            return

        logger.info(
            f"EmbyAutoClean 开始执行，模式：{'预演' if self._dry_run else '真实删除'}，"
            f"阈值：{self._days_threshold} 天，收藏范围：{self._favorite_scope}，"
            f"媒体类型：{self._media_types}"
        )

        for server_name, svc in infos.items():
            try:
                self.__clean_server(server_name, svc)
            except Exception as e:
                logger.error(f"EmbyAutoClean 清理服务器 {server_name} 时发生异常：{e}", exc_info=True)

    def __clean_server(self, server_name: str, svc):
        """
        对单台 Emby 服务器执行清理
        """
        logger.info(f"EmbyAutoClean 开始处理服务器：{server_name}")

        fav_user_ids = self.__get_favorite_user_ids(svc)
        logger.info(f"[{server_name}] 收藏检查用户数：{len(fav_user_ids)}")

        # v1.2.0：选择一个 viewer 用户用于主查询（拿到 UserData 字段做快速剪枝）
        viewer_uid = self.__pick_viewer_uid(svc, fav_user_ids)
        if viewer_uid:
            logger.info(f"[{server_name}] 主查询 viewer_uid={viewer_uid[:8]}...")
        else:
            logger.warning(f"[{server_name}] 未找到合适的 viewer 用户，将使用顶层端点兜底查询")

        library_ids = self.__resolve_library_ids(svc)
        candidates = self.__fetch_candidates(svc, viewer_uid=viewer_uid, library_ids=library_ids)
        logger.info(f"[{server_name}] 获取到候选条目数：{len(candidates)}")

        deleted_count = 0
        skipped_count = 0

        for item in candidates:
            if deleted_count >= self._max_deletions:
                logger.info(f"[{server_name}] 达到单次最大删除数 {self._max_deletions}，停止")
                break

            should_del, reason = self.__should_delete(item, fav_user_ids, svc, viewer_uid)
            if not should_del:
                skipped_count += 1
                logger.debug(f"[{server_name}] 跳过 {item.get('Name')}：{reason}")
                continue

            # 执行删除或预演
            if self._dry_run:
                logger.info(f"[DRY-RUN] [{server_name}] 将删除：{item.get('Name')} ({item.get('ProductionYear')})")
                success = True
            else:
                success = self.__delete_item(svc, item.get("Id"))
                if success:
                    logger.info(f"[{server_name}] 已删除：{item.get('Name')} ({item.get('ProductionYear')})")
                else:
                    logger.warning(f"[{server_name}] 删除失败：{item.get('Name')}")
                # 避免打爆 API
                time.sleep(0.5)

            if success:
                # 逐条通知
                try:
                    self.__notify_one(svc, item, server_name, self._dry_run)
                except Exception as e:
                    logger.warning(f"[{server_name}] 发送通知失败：{e}")

                # 写历史
                try:
                    self.__append_history(svc, item, server_name, self._dry_run)
                except Exception as e:
                    logger.warning(f"[{server_name}] 记录历史失败：{e}")

                deleted_count += 1

        logger.info(
            f"[{server_name}] 处理完成："
            f"{'预演标记' if self._dry_run else '实际删除'}={deleted_count}，跳过={skipped_count}"
        )

    # ───────── Emby API ─────────
    @staticmethod
    def __emby_get(svc, path: str, params: Optional[Dict[str, Any]] = None):
        """
        统一的 GET 调用
        path 以 emby/ 开头即可
        """
        qs = ""
        if params:
            from urllib.parse import urlencode
            qs = "?" + urlencode({k: v for k, v in params.items() if v is not None and v != ""})
        url = f"[HOST]{path}{qs}{'&' if qs else '?'}api_key=[APIKEY]"
        try:
            res = svc.instance.get_data(url=url)
            if res is None:
                logger.warning(f"Emby GET {path} 返回 None（可能是网络错误或超时）")
            return res
        except Exception as e:
            logger.warning(f"Emby GET {path} 异常：{e}")
            return None

    @staticmethod
    def __emby_delete(svc, path: str) -> Tuple[bool, int]:
        """
        统一的 DELETE 调用。返回 (是否成功, http_status)
        """
        url = f"[HOST]{path}?api_key=[APIKEY]"
        try:
            # 优先尝试 instance 自带 delete_data 方法
            if hasattr(svc.instance, "delete_data"):
                res = svc.instance.delete_data(url=url)
            else:
                # 回退：通过 requests 库
                import requests
                real_url = url.replace("[HOST]", str(svc.config.config.get("host", "")).rstrip("/") + "/") \
                              .replace("[APIKEY]", str(svc.config.config.get("apikey", "")))
                logger.debug(f"Emby DELETE 使用 requests 库，URL={real_url[:50]}...")
                res = requests.delete(real_url, timeout=30)
            if res is None:
                logger.warning(f"Emby DELETE {path} 返回 None（可能是网络错误）")
                return False, 0
            code = getattr(res, "status_code", 0)
            success = (200 <= code < 300) or code == 404
            if not success:
                logger.warning(f"Emby DELETE {path} HTTP {code}（非成功状态码）")
            return success, code
        except Exception as e:
            logger.error(f"Emby DELETE {path} 异常：{e}", exc_info=True)
            return False, 0

    def __resolve_library_ids(self, svc) -> List[str]:
        """
        根据 _library_names（多行用户配置）解析成 ParentId 列表；为空则返回空表示不限制
        """
        names = [x.strip() for x in (self._library_names or "").splitlines() if x.strip()]
        if not names:
            return []
        res = self.__emby_get(svc, "emby/Library/MediaFolders")
        if not res:
            return []
        try:
            data = res.json() if hasattr(res, "json") else res
        except Exception:
            return []
        items = (data or {}).get("Items") or []
        name_set = {n.lower() for n in names}
        ids = [it.get("Id") for it in items if str(it.get("Name", "")).lower() in name_set and it.get("Id")]
        logger.info(f"EmbyAutoClean 匹配到媒体库 ID：{ids}")
        return ids

    def __fetch_candidates(self, svc, viewer_uid: Optional[str],
                           library_ids: List[str]) -> List[dict]:
        """
        分页拉取候选条目。

        v1.2.0 修复：使用用户维度端点 emby/Users/{uid}/Items，以便：
        1) 返回的 UserData 字段可直接用于 viewer 快速剪枝
        2) 彻底规避服务端 Filters=IsNotFavorite 在顶层 /emby/Items 上不生效的 bug
        全部条目拉回客户端再做收藏/播放/关键字/标签判定，保证正确性。
        """
        include_types = ",".join(self._media_types or [])
        fields = "DateCreated,UserData,Path,ProviderIds,Tags,Genres,ProductionYear,OriginalTitle"

        if viewer_uid:
            endpoint = f"emby/Users/{viewer_uid}/Items"
        else:
            # 兜底：无任何可用用户时走顶层
            endpoint = "emby/Items"

        all_items: List[dict] = []

        def _query(parent_id: Optional[str]):
            start = 0
            limit = 200
            while True:
                params = {
                    "Recursive": "true",
                    "IncludeItemTypes": include_types,
                    "Fields": fields,
                    "StartIndex": start,
                    "Limit": limit,
                    "SortBy": "DateCreated",
                    "SortOrder": "Ascending",
                }
                if parent_id:
                    params["ParentId"] = parent_id

                res = self.__emby_get(svc, endpoint, params=params)
                if res is None:
                    return
                try:
                    data = res.json() if hasattr(res, "json") else res
                except Exception:
                    return
                items = (data or {}).get("Items") or []
                if not items:
                    return
                all_items.extend(items)
                if len(items) < limit:
                    return
                start += limit

        if library_ids:
            for pid in library_ids:
                _query(pid)
        else:
            _query(None)

        return all_items

    def __get_favorite_user_ids(self, svc) -> List[str]:
        """
        根据 _favorite_scope 返回需要检查收藏状态的用户 ID 列表
        """
        res = self.__emby_get(svc, "emby/Users")
        if not res:
            return []
        try:
            users = res.json() if hasattr(res, "json") else res
        except Exception:
            return []
        if not isinstance(users, list):
            return []

        scope = self._favorite_scope or "any_user"
        if scope == "admin_only":
            return [u.get("Id") for u in users
                    if u.get("Id") and (u.get("Policy") or {}).get("IsAdministrator")]
        if scope == "specific_users":
            names = {n.strip().lower()
                     for n in (self._favorite_users or "").splitlines() if n.strip()}
            return [u.get("Id") for u in users
                    if u.get("Id") and str(u.get("Name", "")).lower() in names]
        # any_user
        return [u.get("Id") for u in users if u.get("Id")]

    def __is_favorited_by_any(self, svc, item_id: str, user_ids: List[str]) -> bool:
        """
        逐用户检查 UserData.IsFavorite，任一 True 即返回 True
        """
        for uid in user_ids:
            res = self.__emby_get(svc, f"emby/Users/{uid}/Items/{item_id}",
                                  params={"Fields": "UserData"})
            if not res:
                continue
            try:
                data = res.json() if hasattr(res, "json") else res
            except Exception:
                continue
            ud = (data or {}).get("UserData") or {}
            if ud.get("IsFavorite"):
                return True
            # 轻微限流
            time.sleep(0.1)
        return False

    def __delete_item(self, svc, item_id: str) -> bool:
        if not item_id:
            return False
        ok, code = self.__emby_delete(svc, f"emby/Items/{item_id}")
        if not ok:
            logger.warning(f"Emby 删除条目 {item_id} 失败，HTTP code={code}")
        else:
            logger.debug(f"Emby 删除条目 {item_id} 成功，code={code}")
        return ok

    # ───────── 判定 ─────────
    def __should_delete(
        self, item: dict, fav_user_ids: List[str], svc, viewer_uid: Optional[str]
    ) -> Tuple[bool, str]:
        ud = item.get("UserData") or {}

        # 1. 日期
        dc_str = item.get("DateCreated")
        if not dc_str:
            return False, "缺少 DateCreated"
        dc = self.__parse_emby_time(dc_str)
        if not dc:
            return False, f"无法解析 DateCreated: {dc_str}"
        threshold = datetime.now(tz=dc.tzinfo) - timedelta(days=self._days_threshold)
        if dc > threshold:
            return False, f"未到期（{dc.date()} > {threshold.date()}）"

        # 2. 已播放（viewer 视角，主查询直接带回）
        if self._skip_if_played:
            if (ud.get("PlayCount") or 0) > 0 or (ud.get("PlaybackPositionTicks") or 0) > 0:
                return False, "已有播放记录（viewer）"

        # 3. viewer 收藏快速剪枝（UserData 已随主查询返回）
        if ud.get("IsFavorite"):
            short_uid = viewer_uid[:8] if viewer_uid else "?"
            return False, f"被 viewer({short_uid}) 收藏"

        # 4. 排除关键字
        kws = [k.strip() for k in (self._exclude_keywords or "").splitlines() if k.strip()]
        if kws:
            name = str(item.get("Name", "")).lower()
            orig = str(item.get("OriginalTitle", "")).lower()
            for kw in kws:
                if kw.lower() in name or kw.lower() in orig:
                    return False, f"命中排除关键字：{kw}"

        # 5. 排除标签
        tags_cfg = {t.strip().lower() for t in (self._exclude_tags or "").splitlines() if t.strip()}
        if tags_cfg:
            item_tags = {str(t).lower() for t in (item.get("Tags") or [])}
            hit = tags_cfg & item_tags
            if hit:
                return False, f"命中排除标签：{','.join(hit)}"

        # 6. 剩余目标用户补查（排除 viewer 本身，只查还没查过的）
        other_uids = [u for u in (fav_user_ids or []) if u and u != viewer_uid]
        if other_uids:
            if self.__is_favorited_by_any(svc, item.get("Id"), other_uids):
                scope_label = {
                    "any_user": "任一用户",
                    "admin_only": "管理员",
                    "specific_users": "指定用户",
                }.get(self._favorite_scope, self._favorite_scope)
                return False, f"被{scope_label}收藏（非 viewer）"

        return True, "符合清理条件"

    def __pick_viewer_uid(self, svc, fav_user_ids: List[str]) -> Optional[str]:
        """
        选择一个 Emby 用户作为主查询 viewer，其 UserData 将随主查询返回。
        策略（v1.2.0）：
            admin_only       -> fav_user_ids[0]（已被过滤为 admin）
            specific_users   -> fav_user_ids[0]
            any_user         -> 优先 admin，找不到取 fav_user_ids[0]
            三者都空         -> None（兜底走顶层 emby/Items）
        """
        if not fav_user_ids:
            return None
        if (self._favorite_scope or "any_user") == "any_user":
            # 重新读一次用户列表找 admin（避免重复缓存复杂度）
            res = self.__emby_get(svc, "emby/Users")
            try:
                users = res.json() if res is not None and hasattr(res, "json") else None
            except Exception:
                users = None
            if isinstance(users, list):
                for u in users:
                    uid = u.get("Id")
                    is_admin = (u.get("Policy") or {}).get("IsAdministrator")
                    if uid and is_admin and uid in fav_user_ids:
                        return uid
        return fav_user_ids[0]

    def __run_diagnose(self, server_name: str, svc):
        """
        v1.2.0 新增：收藏诊断模式
        - 对 fav_user_ids 中的每个用户，调 GET emby/Users/{uid}/Items?Filters=IsFavorite
        - 打印每个用户的完整收藏清单到日志，便于与 Emby UI「最爱」对照验证
        - 若开启通知，发送 1 条汇总通知（截断前 20 条）
        - 全程不触发 DELETE，不写历史
        """
        logger.info(f"[{server_name}] ===== 收藏诊断开始 =====")

        fav_user_ids = self.__get_favorite_user_ids(svc)
        logger.info(f"[{server_name}] scope={self._favorite_scope}，检查用户数={len(fav_user_ids)}")
        if not fav_user_ids:
            logger.warning(f"[{server_name}] 没有可检查的用户（scope 过滤后为空）")
            return

        # 拿用户名映射（uid -> name）
        uid_to_name: Dict[str, str] = {}
        res_users = self.__emby_get(svc, "emby/Users")
        try:
            users = res_users.json() if res_users is not None and hasattr(res_users, "json") else None
        except Exception:
            users = None
        if isinstance(users, list):
            for u in users:
                uid = u.get("Id")
                if uid:
                    uid_to_name[uid] = str(u.get("Name") or "")

        include_types = ",".join(self._media_types or [])
        fields = "DateCreated,UserData,ProductionYear,OriginalTitle"

        all_favorited: Dict[str, dict] = {}  # 去重：item_id -> item
        per_user_counts: List[Tuple[str, str, int]] = []  # (uid, name, count)

        for uid in fav_user_ids:
            uname = uid_to_name.get(uid, uid[:8])
            items_for_user: List[dict] = []
            start = 0
            limit = 200
            while True:
                params = {
                    "Recursive": "true",
                    "IncludeItemTypes": include_types,
                    "Fields": fields,
                    "Filters": "IsFavorite",
                    "StartIndex": start,
                    "Limit": limit,
                    "SortBy": "SortName",
                    "SortOrder": "Ascending",
                }
                res = self.__emby_get(svc, f"emby/Users/{uid}/Items", params=params)
                if res is None:
                    break
                try:
                    data = res.json() if hasattr(res, "json") else res
                except Exception:
                    break
                items = (data or {}).get("Items") or []
                if not items:
                    break
                items_for_user.extend(items)
                if len(items) < limit:
                    break
                start += limit

            per_user_counts.append((uid, uname, len(items_for_user)))
            logger.info(f"[{server_name}] [user={uname} (uid={uid[:8]}...)] 被收藏 {len(items_for_user)} 条：")
            for idx, it in enumerate(items_for_user, 1):
                t = it.get("Type", "")
                n = it.get("Name", "")
                y = it.get("ProductionYear", "")
                logger.info(f"[{server_name}]   {idx}. {t} | {n}" + (f" ({y})" if y else ""))
                # 记录去重并集
                iid = it.get("Id")
                if iid and iid not in all_favorited:
                    all_favorited[iid] = it

        logger.info(
            f"[{server_name}] ===== 合计：按 {self._favorite_scope} 模式会跳过 "
            f"{len(all_favorited)} 条（去重并集） ====="
        )

        # 汇总通知（可选）
        if self._notify:
            try:
                ntype = self.__resolve_notification_type()
                title = "【Emby 收藏诊断】"
                header_lines = [
                    f"服务器：{server_name}",
                    f"scope：{self._favorite_scope}",
                    f"检查用户数：{len(fav_user_ids)}",
                    f"去重并集：{len(all_favorited)} 条",
                    "",
                    "各用户收藏数：",
                ]
                for _, uname, cnt in per_user_counts:
                    header_lines.append(f"  · {uname}：{cnt}")
                header_lines.append("")
                header_lines.append("收藏清单（前 20 条）：")

                preview: List[str] = []
                total_items = list(all_favorited.values())
                for idx, it in enumerate(total_items[:20], 1):
                    n = it.get("Name", "")
                    y = it.get("ProductionYear", "")
                    preview.append(f"  {idx}. {n}" + (f" ({y})" if y else ""))
                remaining = max(0, len(total_items) - 20)
                if remaining:
                    preview.append(f"  …还有 {remaining} 条未显示（完整列表见日志）")

                text = "\n".join(header_lines + preview)
                self.post_message(mtype=ntype, title=title, text=text)
            except Exception as e:
                logger.warning(f"[{server_name}] 诊断汇总通知发送失败：{e}")

        logger.info(f"[{server_name}] ===== 收藏诊断结束 =====")

    @staticmethod
    def __parse_emby_time(s: str) -> Optional[datetime]:
        """
        解析 Emby 返回的 ISO 时间字符串（可能带 Z 或纳秒）
        """
        if not s:
            return None
        s = s.strip()
        # 规范化：去掉 Z，截断到微秒
        s2 = s.replace("Z", "+00:00")
        # 处理过多小数位
        m = re.match(r"(.+?\.\d{1,6})\d*(.*)$", s2)
        if m:
            s2 = m.group(1) + m.group(2)
        try:
            return datetime.fromisoformat(s2)
        except Exception:
            try:
                return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=pytz.UTC)
            except Exception:
                try:
                    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)
                except Exception:
                    return None

    # ───────── 通知 & 历史 ─────────
    def __get_poster_url(self, svc, item_id: str) -> str:
        """
        拼 Emby 海报 URL；使用真实 host/apikey，便于通知端直接展示
        """
        try:
            host = str(getattr(svc.config, "config", {}).get("host", "")).rstrip("/")
            apikey = str(getattr(svc.config, "config", {}).get("apikey", ""))
            if not host:
                return ""
            return f"{host}/emby/Items/{item_id}/Images/Primary?maxHeight=400&api_key={apikey}"
        except Exception:
            return ""

    def __resolve_notification_type(self) -> NotificationType:
        try:
            return NotificationType[self._notify_type]
        except Exception:
            return NotificationType.MediaServer

    def __notify_one(self, svc, item: dict, server_name: str, dry_run: bool):
        if not self._notify:
            return
        ntype = self.__resolve_notification_type()
        type_label = {"Movie": "电影", "Series": "剧集", "Episode": "单集"}.get(
            item.get("Type", ""), item.get("Type", "")
        )
        year = item.get("ProductionYear") or ""
        dc = item.get("DateCreated", "") or ""
        dc_short = dc[:10] if len(dc) >= 10 else dc
        status_text = "将被删除" if dry_run else "已删除"
        title = f"【Emby自动清理{'（预演）' if dry_run else ''}】"
        text = (
            f"服务器：{server_name}\n"
            f"类型：{type_label}\n"
            f"标题：{item.get('Name', '')}" + (f" ({year})" if year else "") + "\n"
            f"添加时间：{dc_short}\n"
            f"状态：{status_text}"
        )
        image = self.__get_poster_url(svc, item.get("Id", ""))
        self.post_message(mtype=ntype, title=title, text=text, image=image or None)

    def __append_history(self, svc, item: dict, server_name: str, dry_run: bool):
        history: List[dict] = self.get_data("history") or []
        history.append({
            "server": server_name,
            "item_id": item.get("Id"),
            "type": item.get("Type"),
            "title": item.get("Name"),
            "year": item.get("ProductionYear"),
            "date_created": item.get("DateCreated"),
            "image": self.__get_poster_url(svc, item.get("Id", "")),
            "dry_run": dry_run,
            "del_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        })
        # 滚动保留
        if len(history) > self._HISTORY_MAX:
            history = history[-self._HISTORY_MAX:]
        self.save_data("history", history)

    # ───────── 表单 ─────────
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 媒体服务器下拉（仅 emby）
        try:
            server_items = [
                {"title": c.name, "value": c.name}
                for c in MediaServerHelper().get_configs().values()
                if getattr(c, "type", None) == "emby"
            ]
        except Exception:
            server_items = []

        # NotificationType 选项
        ntype_items = [
            {"title": "媒体服务器", "value": "MediaServer"},
            {"title": "插件", "value": "Plugin"},
            {"title": "手动", "value": "Manual"},
            {"title": "整理", "value": "Organize"},
        ]

        return [
            {
                'component': 'VForm',
                'content': [
                    # 第 1 行：基础开关
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {'model': 'enabled', 'label': '启用插件'}
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {'model': 'onlyonce', 'label': '立即运行一次'}
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'dry_run',
                                            'label': 'Dry-run 预演（不实际删除）'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'diagnose_favorites',
                                            'label': '收藏诊断（仅输出收藏清单，不删除）'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # 第 1.5 行：诊断提示
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'density': 'compact',
                                            'text': '勾选「收藏诊断」后运行一次（建议配合「立即运行一次」），将不做任何删除，只输出每个用户的收藏清单到日志和通知。用于与 Emby UI 的「最爱」列表对照验证识别是否准确。诊断模式优先级高于 Dry-run。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 第 2 行：定时 & 通知开关 & 通知类型
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 3 * * *'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {'model': 'notify', 'label': '开启通知'}
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'notify_type',
                                            'label': '通知类型',
                                            'items': ntype_items
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # 第 3 行：服务器 + 媒体类型
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'mediaservers',
                                            'label': 'Emby 服务器',
                                            'items': server_items
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'media_types',
                                            'label': '媒体类型',
                                            'items': [
                                                {'title': '电影', 'value': 'Movie'},
                                                {'title': '剧集', 'value': 'Series'},
                                                {'title': '单集', 'value': 'Episode'},
                                            ]
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # 手动 Emby 服务器
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'manual_servers',
                                            'label': '手动 Emby 服务器（未注册到 MoviePilot 的服务器）',
                                            'rows': 3,
                                            'placeholder': '每行一个，格式：名称|URL|APIKey\n'
                                                           '示例：家庭Emby|https://emby.home.lan:8096|abcdef123456'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 第 4 行：规则参数
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'days_threshold',
                                            'label': '添加时间（N天前）',
                                            'type': 'number',
                                            'placeholder': '180'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_deletions',
                                            'label': '单次最大删除数',
                                            'type': 'number',
                                            'placeholder': '50'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'favorite_scope',
                                            'label': '收藏判定范围',
                                            'items': [
                                                {'title': '任一用户收藏即跳过', 'value': 'any_user'},
                                                {'title': '仅管理员收藏才跳过', 'value': 'admin_only'},
                                                {'title': '指定用户收藏才跳过', 'value': 'specific_users'},
                                            ]
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # 第 5 行：跳过已播放（单独）
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'skip_if_played',
                                            'label': '跳过已有播放记录'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # 第 6 行：指定收藏用户
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'favorite_users',
                                            'label': '指定收藏用户名（仅"指定用户"模式生效）',
                                            'rows': 3,
                                            'placeholder': '每行一个 Emby 用户名，这些用户收藏的条目将被跳过'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 第 7 行：限定媒体库
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'library_names',
                                            'label': '限定媒体库（留空=全部）',
                                            'rows': 3,
                                            'placeholder': '每行一个媒体库名称，如：电影、剧集'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # 第 8 行：排除关键字
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'exclude_keywords',
                                            'label': '排除关键字（标题/原名包含即跳过）',
                                            'rows': 4,
                                            'placeholder': '每行一个关键字'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'exclude_tags',
                                            'label': '排除标签（Emby Tags 命中即跳过）',
                                            'rows': 4,
                                            'placeholder': '每行一个标签'
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # 提示
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '首次使用请务必开启 Dry-run 预演，确认扫描结果无误后再关闭。删除 Emby 条目将连带删除底层磁盘文件！手动模式下 APIKey 将明文保存在插件配置中，请仅在可信环境使用。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": True,
            "dry_run": True,
            "diagnose_favorites": False,
            "cron": "0 3 * * *",
            "notify_type": "MediaServer",
            "mediaservers": [],
            "manual_servers": "",
            "library_names": "",
            "media_types": ["Movie", "Series"],
            "days_threshold": 180,
            "max_deletions": 50,
            "favorite_scope": "any_user",
            "favorite_users": "",
            "exclude_keywords": "",
            "exclude_tags": "",
            "skip_if_played": True,
        }

    # ───────── 详情页 ─────────
    def get_page(self) -> List[dict]:
        historys = self.get_data("history") or []
        if not historys:
            return [{
                'component': 'div',
                'props': {'class': 'text-center'},
                'text': '暂无清理记录'
            }]

        historys = sorted(historys, key=lambda x: x.get('del_time', ''), reverse=True)
        type_label = {"Movie": "电影", "Series": "剧集", "Episode": "单集"}

        cards = []
        for h in historys:
            is_dry = h.get("dry_run")
            chip_color = "warning" if is_dry else "error"
            chip_text = "预演" if is_dry else "已删除"

            sub_contents = [
                {
                    'component': 'div',
                    'props': {'class': 'd-flex align-center px-2 pt-2'},
                    'content': [
                        {
                            'component': 'VChip',
                            'props': {
                                'color': chip_color,
                                'size': 'small',
                                'variant': 'tonal',
                                'class': 'mr-2'
                            },
                            'text': chip_text
                        },
                        {
                            'component': 'span',
                            'props': {'class': 'text-caption text-medium-emphasis'},
                            'text': h.get("server") or ""
                        }
                    ]
                },
                {
                    'component': 'VCardText',
                    'props': {'class': 'pa-0 px-2'},
                    'text': f"类型：{type_label.get(h.get('type'), h.get('type') or '')}"
                },
                {
                    'component': 'VCardText',
                    'props': {'class': 'pa-0 px-2'},
                    'text': f"标题：{h.get('title', '')}"
                         + (f" ({h.get('year')})" if h.get('year') else "")
                },
                {
                    'component': 'VCardText',
                    'props': {'class': 'pa-0 px-2'},
                    'text': f"添加时间：{(h.get('date_created') or '')[:10]}"
                },
                {
                    'component': 'VCardText',
                    'props': {'class': 'pa-0 px-2 pb-2'},
                    'text': f"执行时间：{h.get('del_time') or ''}"
                },
            ]

            cards.append({
                'component': 'VCard',
                'content': [
                    {
                        'component': 'div',
                        'props': {
                            'class': 'd-flex justify-space-start flex-nowrap flex-row',
                        },
                        'content': [
                            {
                                'component': 'div',
                                'content': [
                                    {
                                        'component': 'VImg',
                                        'props': {
                                            'src': h.get("image") or "",
                                            'height': 150,
                                            'width': 100,
                                            'aspect-ratio': '2/3',
                                            'class': 'object-cover shadow ring-gray-500',
                                            'cover': True,
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'div',
                                'props': {'class': 'flex-grow-1'},
                                'content': sub_contents
                            }
                        ]
                    }
                ]
            })

        return [{
            'component': 'div',
            'props': {'class': 'grid gap-3 grid-info-card'},
            'content': cards
        }]
