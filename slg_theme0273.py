"""Rose identity tokens, independent of the GUI import and persistent state."""
LIGHT = {
    'BG': '#f8f5f7', 'CARD': '#ffffff', 'CARD_HOVER': '#f6e9ef',
    'SIDEBAR': '#f0eaee', 'ACCENT': '#a12d60', 'ACCENT_HOVER': '#87234f',
    'TEXT': '#252027', 'MUTED': '#685d65', 'CHIP': '#eee2e9',
    'CHIP_OFF': '#f6d9d9', 'PLACEHOLDER': '#dfd5dc', 'DANGER_TEXT': '#8a2b2b',
    'ON_ACCENT': '#ffffff', 'STAR': '#9b6b00',
}
# Exact dark palette of the 0.27.0 baseline (769c126).
DARK = {
    'BG': '#110d15', 'CARD': '#1d1520', 'CARD_HOVER': '#2b1d2b',
    'SIDEBAR': '#100b13', 'ACCENT': '#ef6a9b', 'ACCENT_HOVER': '#fb83af',
    'TEXT': '#f5edf3', 'MUTED': '#b8a8b5', 'CHIP': '#382632',
    'CHIP_OFF': '#512637', 'PLACEHOLDER': '#342a36', 'DANGER_TEXT': '#ff9aa8',
    'ON_ACCENT': '#28131e', 'STAR': '#f2ca77',
}


def palette(mode):
    return dict(DARK if mode == 'dark' else LIGHT)
