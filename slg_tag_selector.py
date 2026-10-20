"""Bounded Tk tag picker; selection survives filtering and pagination."""
import customtkinter as ctk


class TagSelector(ctk.CTkFrame):
    PAGE_SIZE = 50

    def __init__(self, parent, names, current, display, checkbox, **kwargs):
        super().__init__(parent, fg_color="transparent", **kwargs)
        self.names = list(dict.fromkeys(names))
        self.display, self.checkbox = display, checkbox
        self.variables = {name: ctk.BooleanVar(self, value=True) for name in current}
        self.page = 0
        self.rendering = True
        self._pending = None
        self.search = ctk.CTkEntry(self, placeholder_text="搜索标签")
        self.search.pack(fill="x", pady=(4, 4))
        self.search.bind("<KeyRelease>", self._search_changed)
        self.summary = ctk.CTkLabel(self, text="标签加载中…", anchor="w")
        self.summary.pack(fill="x")
        self.options = ctk.CTkFrame(self, fg_color="transparent")
        self.options.pack(fill="x")
        nav = ctk.CTkFrame(self, fg_color="transparent")
        nav.pack(fill="x", pady=4)
        self.previous = ctk.CTkButton(nav, text="上一页", width=80, command=lambda: self.turn(-1))
        self.previous.pack(side="left")
        self.next = ctk.CTkButton(nav, text="下一页", width=80, command=lambda: self.turn(1))
        self.next.pack(side="right")
        self._pending = self.after(100, self.render)

    def _search_changed(self, _event=None):
        self.page = 0
        if self._pending is not None:
            self.after_cancel(self._pending)
        self._pending = self.after(150, self.render)

    def turn(self, delta):
        self.page = max(0, self.page + delta)
        self.render()

    def render(self):
        self.rendering = True
        if self._pending is not None:
            self.after_cancel(self._pending)
            self._pending = None
        query = self.search.get().strip().casefold()
        names = [n for n in self.names if not query or query in n.casefold()
                 or query in self.display(n).casefold()]
        last = max(0, (len(names) - 1) // self.PAGE_SIZE)
        self.page = min(self.page, last)
        for child in self.options.winfo_children():
            child.destroy()
        start = self.page * self.PAGE_SIZE
        visible = names[start:start + self.PAGE_SIZE]
        for column in range(2):
            self.options.grid_columnconfigure(column, weight=1)
        index = [0]
        def batch():
            self._pending = None
            # Four widgets cap each Tk turn; no growing full-list flow layout.
            end = min(len(visible), index[0] + 4)
            while index[0] < end:
                n = index[0]
                name = visible[n]
                if name not in self.variables:
                    self.variables[name] = ctk.BooleanVar(self, value=False)
                self.checkbox(self.options, self.display(name), self.variables[name]).grid(
                    row=n // 2, column=n % 2, sticky="w", padx=2, pady=2)
                index[0] += 1
            if index[0] < len(visible):
                self._pending = self.after(1, batch)
            else:
                self.rendering = False
        self._pending = self.after(1, batch)
        selected = sum(bool(v.get()) for v in self.variables.values())
        self.summary.configure(text="%d 个标签 · 已选 %d · 第 %d/%d 页" % (
            len(names), selected, self.page + 1, last + 1))
        self.previous.configure(state="normal" if self.page else "disabled")
        self.next.configure(state="normal" if self.page < last else "disabled")

    def add_tag(self, name):
        if name not in self.names:
            self.names.insert(0, name)
        if name not in self.variables:
            self.variables[name] = ctk.BooleanVar(self, value=True)
        else:
            self.variables[name].set(True)
        self.search.delete(0, "end")
        self.search.insert(0, name)
        self.page = 0
        self.render()

    def destroy(self):
        if self._pending is not None:
            self.after_cancel(self._pending)
            self._pending = None
        super().destroy()
