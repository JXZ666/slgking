package com.jxz666.slgking.ui.tools

internal object SyncStagePolicy {
    fun label(catalog: String, phase: String, done: Long, total: Long?, unit: String): String {
        val name = when (catalog) { "dlsite" -> "DLsite"; "western_slg" -> "SLG大类"; else -> "目录" }
        val phaseName = when (phase) {
            "manifest", "checking", "check" -> "检查目录"
            "download", "snapshot", "download_snapshot" -> "下载目录"
            "validate", "verify", "validation" -> "校验数据"
            "merge", "import", "commit", "merging" -> "合并目录"
            "cover", "covers", "download_covers" -> "缓存封面"
            "cover_index" -> "封面索引"
            "source_status" -> "采集状态"
            "complete", "done" -> "完成"
            else -> "正在处理"
        }
        fun amount(n: Long): String = if (unit == "bytes") {
            when {
                n >= 1024 * 1024 -> "%.1f MiB".format(java.util.Locale.ROOT, n / 1048576.0)
                n >= 1024 -> "%.1f KiB".format(java.util.Locale.ROOT, n / 1024.0)
                else -> "$n B"
            }
        } else "$n" + when (unit) { "items", "games", "rows" -> "条"; "files", "covers" -> "个"; "images" -> "张"; else -> "" }
        val count = amount(done.coerceAtLeast(0)) + if (total != null && total > 0) " / ${amount(total)}" else "（总量未知）"
        return "$name · $phaseName · $count"
    }
    fun progress(done: Long, total: Long?): Pair<Long, Long>? =
        total?.takeIf { it > 0 }?.let { done.coerceIn(0, it) to it }
}

internal data class HelpTopic(val title: String, val body: String)
internal val androidHelp = listOf(
    HelpTopic("目录与日期筛选", "SLG大类、DLsite和全部分别保存检索筛选。名称、标签、状态与更新日期可以组合；近7/30/90天及自定义范围采用北京时间自然日，含开始与结束当天。DLsite仅使用作品更新日期，未知日期不混用发行日。"),
    HelpTopic("完整详情、标签与翻译", "简介默认6行，可展开全文；备注独立折叠。标签多行显示，展开可查看全部。原文/中文手动切换，优先使用有效缓存，缺少译文时才翻译；免费或API引擎在工具翻译设置配置。API可能产生服务商费用。可编辑本机标题、简介译文及标签，恢复自动译文不会覆盖原文。DLsite缺少简介时请查看商品页。"),
    HelpTopic("追踪与我的记录", "追踪包含版本更新、想玩和收藏夹，属于个人清单，不代表手机已安装或可运行。我的记录可保存手机移植游戏的名称、版本、备注和HTTP/HTTPS来源网址。APK不扫描应用、不启动游戏、不管理安装包或PC存档。"),
    HelpTopic("隐藏与恢复", "详情可以隐藏不想看的作品，数据不会删除。在工具的隐藏条目管理中恢复；恢复后再次出现在目录和个人清单。"),
    HelpTopic("个人页、头像与隐私", "个人页显示本机已知资料，不在切页或返回前台时自动联网；点击刷新资料或具体功能才加载。本机头像仅在本人设备显示，不上传，不包含在JSON备份；公开名片、评论和榜单显示昵称首字。公开名片开关须经服务器确认，关闭后公开展示按隐私规则过滤。"),
    HelpTopic("评论与追评", "公开评论需登录并审核，私人评论仅保存在本机。追评默认折叠，打开后分页加载；可以发表、删除自己的追评或举报，不提供追评点赞、编辑及积分奖励。发送失败保留输入，可重试。"),
    HelpTopic("消息与活动", "个人页消息入口包含公告、活动和回复，打开相应资源才加载，没有后台轮询。打开消息列表不会标记回复已读，成功打开目标讨论才标记对应消息。活动是否可领取以服务器资格为准。"),
    HelpTopic("同步与停止", "更新所选目录；全部按顺序更新两种目录，各自报告结果。显示当前来源、阶段与实际下载量，总量未知时没有百分比。停止后不继续后续步骤，已提交目录保留；正在原子合并时须等待安全完成。"),
    HelpTopic("备份、账号与数据来源", "个人评分、备注、收藏、自建记录等保存在应用私有目录。手机与PC通过备份迁移，同账号登录不会自动同步本机数据。导入可能覆盖本机记录，请先导出。头像不纳入备份。目录与封面来自项目服务器公开资源，本应用不托管游戏文件；游戏详情提供已有公开来源入口。"),
    HelpTopic("签到与每日任务", "签到与抽奖按北京时间每日00:00刷新；每日任务04:00刷新，每周任务为周一04:00。实际可领取状态以服务器为准。")
)
internal fun searchHelp(query: String): List<HelpTopic> = query.trim().let { q ->
    androidHelp.filter { q.isEmpty() || it.title.contains(q, true) || it.body.contains(q, true) }
}
