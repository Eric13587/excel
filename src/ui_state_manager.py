"""UI State Manager for LoanMaster.

This module provides centralized state management for the Dashboard view,
including selection state, filter state, and card visibility management.
"""
from typing import Optional, List, Any, Callable


class UIStateManager:
    """Manages UI state for the Dashboard view.
    
    Centralizes selection state, filter state, and visibility management
    that was previously embedded in the Dashboard class.
    
    Attributes:
        selected_card: Currently selected IndividualCard widget.
        card_widgets: List of all IndividualCard widgets.
        filter_text: Current filter/search text.
        on_selection_changed: Callback when selection changes.
    """
    
    def __init__(self, on_selection_changed: Callable[[Any], None] = None):
        """Initialize UIStateManager.
        
        Args:
            on_selection_changed: Optional callback invoked when selection changes.
        """
        self._selected_card = None
        self._card_widgets: List[Any] = []
        self._filter_text: str = ""
        self.on_selection_changed = on_selection_changed
    
    @property
    def selected_card(self):
        """Get the currently selected card."""
        return self._selected_card
    
    @property
    def card_widgets(self) -> List[Any]:
        """Get all card widgets."""
        return self._card_widgets
    
    @property
    def filter_text(self) -> str:
        """Get the current filter text."""
        return self._filter_text
    
    def has_selection(self) -> bool:
        """Check if there is a current selection."""
        return self._selected_card is not None
    
    def get_selected_id(self) -> Optional[int]:
        """Get the ID of the selected individual.
        
        Returns:
            Individual ID or None if no selection.
        """
        if self._selected_card:
            return self._selected_card.ind_id
        return None
    
    def get_selected_name(self) -> Optional[str]:
        """Get the name of the selected individual.
        
        Returns:
            Individual name or None if no selection.
        """
        if self._selected_card:
            return self._selected_card.name
        return None
    
    def select(self, card) -> None:
        """Select a card, deselecting the previous one.
        
        Args:
            card: The IndividualCard to select, or None to clear selection.
        """
        # Deselect previous
        if self._selected_card:
            self._selected_card.set_selected(False)
        
        # Select new
        self._selected_card = card
        if card:
            card.set_selected(True)
        
        # Notify listener
        if self.on_selection_changed:
            self.on_selection_changed(card)
    
    def clear_selection(self) -> None:
        """Clear the current selection."""
        self.select(None)
    
    def set_cards(self, cards: List[Any]) -> None:
        """Set the list of cards.
        
        Args:
            cards: List of IndividualCard widgets.
        """
        self._card_widgets = cards
        self._selected_card = None
    
    def add_card(self, card) -> None:
        """Add a card to the list.
        
        Args:
            card: IndividualCard to add.
        """
        self._card_widgets.append(card)
    
    def clear_cards(self) -> None:
        """Clear all cards and selection."""
        self._selected_card = None
        self._card_widgets.clear()
    
    def apply_filter(self, text: str) -> None:
        """Apply a filter to card visibility.
        
        Filters cards by name or phone number containing the search text.
        
        Args:
            text: Search text to filter by.
        """
        self._filter_text = text
        search_text = text.lower()
        
        for card in self._card_widgets:
            name_match = search_text in card.name.lower()
            phone_match = bool(card.phone and search_text in card.phone.lower())
            card.setVisible(name_match or phone_match)
    
    def refresh_filter(self) -> None:
        """Re-apply the current filter."""
        if self._filter_text:
            self.apply_filter(self._filter_text)
    
    def get_visible_cards(self) -> List[Any]:
        """Get all currently visible cards.
        
        Returns:
            List of visible IndividualCard widgets.
        """
        return [card for card in self._card_widgets if card.isVisible()]
    
    def get_all_ids(self) -> List[int]:
        """Get IDs of all cards.
        
        Returns:
            List of individual IDs.
        """
        return [card.ind_id for card in self._card_widgets]
    
    def get_visible_ids(self) -> List[int]:
        """Get IDs of all visible cards.
        
        Returns:
            List of visible individual IDs.
        """
        return [card.ind_id for card in self._card_widgets if card.isVisible()]
