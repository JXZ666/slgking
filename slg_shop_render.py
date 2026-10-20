"""Pure shop view projection; no network, wallet writes or Tk operations."""
import time


class RenderBatch:
    """Cancelable UI-thread work with an epoch guard and a 6 ms work budget.

    An expensive item may exceed the budget, but never shares that turn with
    another item. A 1 ms continuation lets native painting/input run between
    cards. The owner supplies page/identity validity and handles errors.
    """
    def __init__(self, after, cancel, *, budget_ms=6, clock=time.monotonic):
        self.after, self.after_cancel = after, cancel
        self.budget = budget_ms / 1000
        self.clock = clock
        self.job = None
        self.epoch = 0

    def cancel(self):
        self.epoch += 1
        job, self.job = self.job, None
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:
                pass

    def start(self, work, apply, done, *, valid=lambda: True, error=None):
        self.cancel()
        epoch = self.epoch
        iterator = iter(work)

        def tick():
            if epoch != self.epoch:
                return
            self.job = None
            if not valid():
                return
            start = self.clock()
            while True:
                try:
                    item = next(iterator)
                except StopIteration:
                    done()
                    return
                try:
                    apply(item)
                except Exception as exc:
                    self.cancel()
                    if error is not None:
                        error(exc)
                    else:
                        raise
                    return
                if epoch != self.epoch or not valid():
                    return
                if self.clock() - start >= self.budget:
                    break
            self.job = self.after(1, tick)

        self.job = self.after(1, tick)


def reserve_flow(flow, height=None):
    """Keep the full shelf extent stable while partial cards are painted."""
    if not hasattr(flow, '_shop_original_layout'):
        flow._shop_original_layout = flow._layout

        def layout(width=None):
            flow._shop_original_layout(width)
            reserved = getattr(flow, '_shop_reserved_height', None)
            if reserved is not None and flow.cget('height') < reserved:
                flow.configure(height=reserved)
        flow._layout = layout
    was_reserved = getattr(flow, '_shop_reserved_height', None)
    flow._shop_reserved_height = height
    if height is not None:
        flow.configure(height=max(height, flow.cget('height')))
    elif was_reserved is not None:
        flow._layout()


def product_action(item, owned, equipped, points, *, cloud=False, busy=False):
    has = item['id'] in owned
    equipped = equipped if isinstance(equipped, tuple) else (equipped, '')
    appearances = equipped[2] if len(equipped) > 2 and isinstance(equipped[2], dict) else {}
    slot = item.get('appearance') or 'comment_frame'
    active = appearances.get(slot) or (equipped[1] if len(equipped) > 1 and slot == 'comment_frame' else '')
    using = has and item['id'] == (active if item['kind'] == 'decoration' else equipped[0])
    cost = item.get('cost') or 0
    unknown = points is None
    short = 0 if unknown or has or item.get('locked') else max(0, cost - points)
    action, text = 'buy', '兑换'
    if item.get('locked'):
        action, text = 'note', '即将开放'
    elif item.get('cloud_only') and not cloud:
        action, text = 'login', '登录后购买'
    elif item['kind'] == 'makeup':
        action, text = ('note', '云端补签暂未开放') if cloud else ('makeup', '补签')
    elif has:
        action, text = ('equip', '卸下' if using else '装备') if item['kind'] == 'decoration' else ('note', '使用中' if using else '已拥有')
    price = '积分读取中…' if unknown else '还差 %d 分' % short if short else '%d 积分' % cost
    return dict(has=has, using=using, action=action, text=text, short=short,
                price=price, fraction=min(1., points / cost) if not unknown and cost else 0.,
                enabled=action != 'note' and not busy and not (unknown and action != 'login'))


def responsive_columns(available, maximum, minimum, gap=8, previous=None, hysteresis=24):
    """Change columns only after crossing a logical-pixel hysteresis boundary."""
    count = max(1, int((available + gap) // (maximum + gap)))
    if count == 1 and available >= 2 * minimum + gap:
        count = 2
    if previous:
        old_count = previous[0]
        if count > old_count:
            threshold = 2 * minimum + gap if old_count == 1 else (old_count + 1) * maximum + old_count * gap
            if available < threshold + hysteresis:
                count = old_count
        elif count < old_count:
            threshold = 2 * minimum + gap if old_count == 2 else old_count * maximum + (old_count - 1) * gap
            if available > threshold - hysteresis and available >= old_count * minimum + (old_count - 1) * gap:
                count = old_count
    width = max(minimum, min(maximum, int((available - gap * (count - 1)) // count)))
    return count, width
