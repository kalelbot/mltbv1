from asyncio import wait_for, Event, wrap_future, sleep
from functools import partial
from pyrogram.filters import regex, user
from pyrogram.handlers import CallbackQueryHandler
from time import time

from bot import (
    task_dict,
    task_dict_lock,
    LOGGER,
    non_queued_dl,
    queue_dict_lock,
    jd_lock,
    jd_downloads,
)
from bot.helper.ext_utils.bot_utils import new_thread, retry_function
from bot.helper.ext_utils.jdownloader_booter import jdownloader
from bot.helper.ext_utils.task_manager import is_queued, stop_duplicate_check
from bot.helper.listeners.jdownloader_listener import onDownloadStart
from bot.helper.mirror_utils.status_utils.jdownloader_status import JDownloaderStatus
from bot.helper.mirror_utils.status_utils.queue_status import QueueStatus
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.message_utils import (
    sendMessage,
    sendStatusMessage,
    editMessage,
    deleteMessage,
)


async def configureDownload(_, query, obj):
    data = query.data.split()
    message = query.message
    await query.answer()
    if data[1] == "sdone":
        obj.event.set()
    elif data[1] == "cancel":
        await editMessage(message, "Task has been cancelled.")
        obj.is_cancelled = True
        obj.event.set()


class JDownloaderHelper:
    def __init__(self, listener):
        self._listener = listener
        self._timeout = 300
        self._reply_to = ""
        self.event = Event()
        self.is_cancelled = False

    @new_thread
    async def _event_handler(self):
        pfunc = partial(configureDownload, obj=self)
        handler = self._listener.client.add_handler(
            CallbackQueryHandler(
                pfunc, filters=regex("^jdq") & user(self._listener.user_id)
            ),
            group=-1,
        )
        try:
            await wait_for(self.event.wait(), timeout=self._timeout)
        except:
            await editMessage(self._reply_to, "Timed Out. Task has been cancelled!")
            self.is_cancelled = True
            self.event.set()
        finally:
            self._listener.client.remove_handler(*handler)

    async def waitForConfigurations(self):
        future = self._event_handler()
        buttons = ButtonMaker()
        buttons.ubutton("Select", "https://my.jdownloader.org")
        buttons.ibutton("Done Selecting", "jdq sdone")
        buttons.ibutton("Cancel", "jdq cancel")
        button = buttons.build_menu(2)
        msg = f"Disable the unwanted files or change variants from myJdownloader site for <b>{self._listener.name}</b> but don't start it manually!\n\nAfter finish press Done Selecting!\nTimeout: 300s"
        self._reply_to = await sendMessage(self._listener.message, msg, button)
        await wrap_future(future)
        if not self.is_cancelled:
            await deleteMessage(self._reply_to)
        return self.is_cancelled


async def add_jd_download(listener, path):
    async with jd_lock:
        if jdownloader.device is None:
            await listener.onDownloadError(jdownloader.error)
            return

        if not jd_downloads:
            await retry_function(jdownloader.device.linkgrabber.clear_list)
            if odl := await retry_function(
                jdownloader.device.downloads.query_packages, [{}]
            ):
                odl_list = [od["uuid"] for od in odl]
                await retry_function(
                    jdownloader.device.downloads.remove_links,
                    package_ids=odl_list,
                )

        await retry_function(
            jdownloader.device.linkgrabber.add_links,
            [
                {
                    "autoExtract": False,
                    "links": listener.link,
                    "packageName": listener.name or None,
                }
            ],
        )

        await sleep(0.5)
        while await retry_function(jdownloader.device.linkgrabber.is_collecting):
            pass

        start_time = time()
        while (time() - start_time) < 60:
            queued_downloads = await retry_function(
                jdownloader.device.linkgrabber.query_packages,
                [
                    {
                        "bytesTotal": True,
                        "saveTo": True,
                        "availableOnlineCount": True,
                        "availableTempUnknownCount": True,
                        "availableUnknownCount": True,
                    }
                ],
            )

            online_packages = []
            size = 0
            corrupted_packages = []
            gid = 0
            remove_unknown = False
            name = ""
            for pack in queued_downloads:
                online = pack.get("onlineCount", 1)
                if online == 0:
                    LOGGER.error(f"{pack.get('name', '')}. link: {listener.link}")
                    corrupted_packages.append(pack["uuid"])
                    continue
                save_to = pack["saveTo"]
                if gid == 0:
                    gid = pack["uuid"]
                    jd_downloads[gid] = {"status": "collect"}
                    name = save_to.replace("/root/Downloads/", "", 1).split("/", 1)[0]

                if (
                    pack.get("tempUnknownCount", 0) > 0
                    or pack.get("unknownCount", 0) > 0
                ):
                    remove_unknown = True

                size += pack.get("bytesTotal", 0)
                online_packages.append(pack["uuid"])
                if save_to.startswith("/root/Downloads/"):
                    await retry_function(
                        jdownloader.device.linkgrabber.set_download_directory,
                        save_to.replace("/root/Downloads", path, 1),
                        [pack["uuid"]],
                    )

            if online_packages:
                break
        else:
            error = (
                name or "Download Not Added! Maybe some issues in jdownloader or site!"
            )
            await listener.onDownloadError(error)
            return

        jd_downloads[gid]["ids"] = online_packages

        corrupted_links = []
        if remove_unknown:
            links = await retry_function(
                jdownloader.device.linkgrabber.query_links,
                [{"packageUUIDs": online_packages, "availability": True}],
            )
            corrupted_links = [
                link["uuid"]
                for link in links
                if link["availability"].lower() != "online"
            ]
        if corrupted_packages or corrupted_links:
            await retry_function(
                jdownloader.device.linkgrabber.remove_links,
                corrupted_links,
                corrupted_packages,
            )

    listener.name = listener.name or name

    msg, button = await stop_duplicate_check(listener)
    if msg:
        await listener.onDownloadError(msg, button)
        return

    if listener.select and await JDownloaderHelper(listener).waitForConfigurations():
        await retry_function(
            jdownloader.device.linkgrabber.remove_links,
            package_ids=online_packages,
        )
        listener.removeFromSameDir()
        return

    add_to_queue, event = await is_queued(listener.mid)
    if add_to_queue:
        LOGGER.info(f"Added to Queue/Download: {listener.name}")
        async with task_dict_lock:
            task_dict[listener.mid] = QueueStatus(listener, size, f"{gid}", "dl")
        await listener.onDownloadStart()
        if listener.multi <= 1:
            await sendStatusMessage(listener.message)
        await event.wait()
        async with task_dict_lock:
            if listener.mid not in task_dict:
                return
        from_queue = True
    else:
        from_queue = False

    await retry_function(
        jdownloader.device.linkgrabber.move_to_downloadlist,
        package_ids=online_packages,
    )

    await sleep(0.5)

    download_packages = await retry_function(
        jdownloader.device.downloads.query_packages,
        [{"saveTo": True}],
    )
    async with jd_lock:
        packages = []
        for pack in download_packages:
            if pack["saveTo"].startswith(path):
                if not packages:
                    del jd_downloads[gid]
                    gid = pack["uuid"]
                    jd_downloads[gid] = {"status": "down"}
                packages.append(pack["uuid"])
        if packages:
            jd_downloads[gid]["ids"] = packages

    if not packages:
        await listener.onDownloadError("This Download have been removed manually!")
        return

    await retry_function(
        jdownloader.device.downloads.force_download,
        package_ids=packages,
    )

    async with task_dict_lock:
        task_dict[listener.mid] = JDownloaderStatus(listener, f"{gid}")

    async with queue_dict_lock:
        non_queued_dl.add(listener.mid)

    await onDownloadStart()

    if from_queue:
        LOGGER.info(f"Start Queued Download from JDownloader: {listener.name}")
    else:
        LOGGER.info(f"Download with JDownloader: {listener.name}")
        await listener.onDownloadStart()
        if listener.multi <= 1:
            await sendStatusMessage(listener.message)
