from PyQt6.QtGui import QColor

class Theme:
    LIGHT = {
        "bg_primary": "#F3F4F6",      # Main background (light gray)
        "bg_secondary": "#FFFFFF",    # Cards, Sidebar (white)
        "bg_header": "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #141E30, stop:1 #243B55)", # Corporate Blue Gradient
        "text_primary": "#1F2937",    # Dark Gray/Black
        "text_secondary": "#4B5563",  # Medium Gray
        "text_header": "#FFFFFF",     # White text on dark header
        "accent": "#3B82F6",          # Primary Blue
        "accent_hover": "#2563eb",
        "border": "#E5E7EB",          # Light Border
        "input_bg": "#FFFFFF",
        "card_bg": "#FFFFFF",
        "card_border": "#E5E7EB",
        "card_hover_border": "#3B82F6",
        "card_selected_bg": "#EFF6FF",
        "card_selected_border": "#3B82F6",
        "success": "#10B981",
        "danger": "#EF4444",
        "warning": "#F59E0B", # Amber 500
        "info": "#0EA5E9",    # Sky 500
        "purple": "#8B5CF6",  # Violet 500
        "danger_bg": "#FEF2F2",
        "warning_bg": "#FFFBEB", # Amber 50
        "success_bg": "#ECFDF5", # Emerald 50
        "danger_border": "#FEE2E2",
        "shadow": "rgba(0, 0, 0, 15)",
        "scrollbar_bg": "#E5E7EB",
        "scrollbar_handle": "#9CA3AF"
    }

    DARK = {
        "bg_primary": "#111827",      # Very Dark Gray/Black
        "bg_secondary": "#1F2937",    # Dark Gray (Cards)
        # Let's keep a subtle gradient or solid dark for header in dark mode
        "bg_header": "qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0f172a, stop:1 #1e293b)", 
        "text_primary": "#F9FAFB",    # Off-white
        "text_secondary": "#9CA3AF",  # Light Gray
        "text_header": "#FFFFFF",
        "accent": "#60A5FA",          # Lighter Blue for Dark Mode
        "accent_hover": "#3B82F6",
        "border": "#374151",          # Dark Border
        "input_bg": "#374151",
        "card_bg": "#1F2937",
        "card_border": "#374151",
        "card_hover_border": "#60A5FA",
        "card_selected_bg": "#1e293b", # Slightly different dark
        "card_selected_border": "#60A5FA",
        "success": "#34D399",
        "danger": "#F87171",
        "warning": "#FBBF24", # Amber 400
        "info": "#38BDF8",    # Sky 400
        "purple": "#A78BFA",  # Violet 400
        "danger_bg": "#3f1d1d",       # Dark Red bg
        "warning_bg": "#451a03",      # Amber 950
        "success_bg": "#022c22",      # Emerald 950
        "danger_border": "#7f1d1d",
        "shadow": "rgba(0, 0, 0, 150)", # Stronger shadow for dark mode
        "scrollbar_bg": "#374151",
        "scrollbar_handle": "#4B5563"
    }

class ThemeManager:
    def __init__(self, db_manager):
        self.db = db_manager
        self.current_theme_name = self.db.get_setting("app_theme", "Light") # Default to Light
        self.colors = Theme.LIGHT if self.current_theme_name == "Light" else Theme.DARK

    def set_theme(self, theme_name):
        self.current_theme_name = theme_name
        self.db.set_setting("app_theme", theme_name)
        self.colors = Theme.LIGHT if theme_name == "Light" else Theme.DARK
    
    def toggle_theme(self):
        new_theme = "Dark" if self.current_theme_name == "Light" else "Light"
        self.set_theme(new_theme)
        return new_theme

    def get_color(self, key):
        return self.colors.get(key, "#ff0000") # Return red if key missing
    
    @property
    def is_dark(self):
        return self.current_theme_name == "Dark"
