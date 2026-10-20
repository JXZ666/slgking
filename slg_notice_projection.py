"""Pure account/channel-aware summary for the notification bell."""


def notice_projection(config, status, *, logged_in=False, developer=False,
                      test_build=True, identity_valid=False, unread=False):
    config = config if isinstance(config, dict) else {}
    current = config.get('announcement')
    current = current if isinstance(current, dict) else {}
    title = str(current.get('title') or '公告与版本说明')[:100]
    result = dict(announcements=[dict(id=current.get('id'),
        title=title + (' · 新公告' if unread else ''))], claimable=False,
        activity_id=None, activity_text='活动状态暂未同步')
    if not logged_in:
        result['activity_text'] = '登录后查看可领取的活动积分'
        return result
    if developer:
        result['activity_text'] = '管理员身份不参与普通账号积分活动'
        return result
    if not identity_valid or not isinstance(status, dict):
        return result
    if status.get('error'):
        result['activity_text'] = '活动状态读取失败，可重新打开消息重试'
        return result
    campaign = status.get('campaign')
    if not isinstance(campaign, dict) or campaign.get('active') is not True:
        result['activity_text'] = '当前没有开放中的积分活动'
        return result
    result['activity_id'] = current.get('id')
    if status.get('claimed') is True:
        result['activity_text'] = '本次活动积分已领取'
        return result
    if status.get('claimed') is not False:
        return result
    if test_build and not (status.get('claimable') is True or campaign.get('claimable') is True):
        result['activity_text'] = '当前测试渠道不可领取本次活动积分'
        return result
    amount = campaign.get('amount')
    if (not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0
            or not isinstance(campaign.get('id'), str) or not campaign['id']):
        return result
    result.update(claimable=True, activity_text='有 %d 活动积分待领取' % amount)
    return result
