"""Stage-aware sync feedback; unknown work is never shown as a percentage."""
import math

PHASES = dict(manifest="检查目录版本", download="下载目录", verify="校验数据",
              merge="合并本机目录", cover_index="更新封面索引", covers="下载封面",
              source_status="读取来源状态", complete="更新结束")


def stage_display(event):
    source = "DLsite" if event.get("catalog_id") == "dlsite" else "SLG大类"
    phase = PHASES.get(event.get("phase"), "处理目录")
    status = event.get("status")
    text = source + " · " + phase
    if event.get("phase") == "merge" and status == "running":
        text += "（当前事务安全完成后可停止）"
    done, total = event.get("done"), event.get("total")
    ratio = None
    valid = lambda v: isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v)
    if valid(done) and valid(total) and total > 0 and 0 <= done <= total:
        ratio = done / total
        if event.get("unit") == "bytes":
            text += " · %.1f/%.1f MiB" % (done / 1048576, total / 1048576)
        else:
            text += " · %d/%d" % (done, total)
    if status in ("failed", "cancelled", "unchanged"):
        text += {"failed": " · 失败", "cancelled": " · 已停止", "unchanged": " · 已是最新"}[status]
    return text, ratio


class SyncProgressUI:
    def _show_sync_stage(self, event):
        if (not self.busy or event.get("job_id") != getattr(self, "_job_generation", None)):
            return
        self._sync_stage_event = dict(event)
        text, fraction = stage_display(event)
        self._set_progress(text)
        bar = getattr(self, "progress_bar", None)
        if bar is None or not bar.winfo_exists():
            return
        bar.stop()
        running = event.get("status") == "running"
        if fraction is not None:
            bar.configure(mode="determinate")
            bar.set(fraction)
        elif running and not self.reduced_motion:
            bar.configure(mode="indeterminate")
            bar.start()
        else:
            bar.configure(mode="determinate")
            bar.set(0)
