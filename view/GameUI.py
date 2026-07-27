
class game_ui:
    
    def get_card_selection(self, hand):
        while True:
            print("\nSelect a card:")
    
            for i, card in enumerate(hand, start=1):
                print(f"[{i}] {card.name}")
    
            choice = input("Enter card number: ")
    
            if not choice.isdigit():
                print("Please enter a valid number.")
                continue
    
            choice = int(choice)
    
            if 1 <= choice <= len(hand):
                return hand[choice - 1]
    
            print("Invalid selection.")
    
    def get_main_phase_action(self, player):
        print("\n=== MAIN PHASE ===")
        print("[1] Play Land")
        print("[2] Cast Spell")
        print("[3] Activate Ability")
    
        while True:
            choice = input("Choose an action: ")
    
            if choice == "1":
                return "play_land"
            elif choice == "2":
                return "cast_spell"
            elif choice == "3":
                return "activate_ability"
    
            print("Invalid choice.")