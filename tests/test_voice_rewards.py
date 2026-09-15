"""Test voice reward calculations and logic."""
import unittest
from utils import rewards


class TestVoiceRewards(unittest.TestCase):
    """Test the voice reward mathematics."""

    def test_voice_presence_calculations(self):
        """Test that voice presence factors are calculated correctly."""
        # Normal user should get full presence
        presence = rewards.voice_presence(
            self_deaf=False, deaf=False, mute=False, 
            self_mute=False, suppress=False
        )
        self.assertEqual(presence, 1.0)
        
        # Self-muted should get half
        presence = rewards.voice_presence(
            self_deaf=False, deaf=False, mute=False, 
            self_mute=True, suppress=False
        )
        self.assertEqual(presence, rewards.VC_SELF_MUTE_FACTOR)
        
        # Deafened should get zero
        presence = rewards.voice_presence(
            self_deaf=True, deaf=False, mute=False, 
            self_mute=False, suppress=False
        )
        self.assertEqual(presence, 0.0)
        
        # Server-muted should get zero
        presence = rewards.voice_presence(
            self_deaf=False, deaf=False, mute=True, 
            self_mute=False, suppress=False
        )
        self.assertEqual(presence, 0.0)

    def test_voice_company_factor(self):
        """Test that company factors encourage group voice chat."""
        # Solo (0 companions) should get reduced rate
        solo_factor = rewards.voice_company_factor(0)
        self.assertEqual(solo_factor, 0.35)
        
        # One companion should get decent rate
        pair_factor = rewards.voice_company_factor(1)
        self.assertGreater(pair_factor, solo_factor)
        
        # Four companions should get full rate
        group_factor = rewards.voice_company_factor(4)
        self.assertEqual(group_factor, 1.0)

    def test_voice_freshness(self):
        """Test that channel freshness decays over time."""
        # Fresh channel (recent activity) should get full rate
        fresh = rewards.voice_freshness(0)
        self.assertEqual(fresh, 1.0)
        
        # Channel within full window should get full rate
        recent = rewards.voice_freshness(rewards.VC_FULL_SECONDS - 100)
        self.assertEqual(recent, 1.0)
        
        # Dead channel should get zero
        dead = rewards.voice_freshness(rewards.VC_DEAD_SECONDS + 100)
        self.assertEqual(dead, 0.0)

    def test_voice_factor_combination(self):
        """Test that combined voice factor works correctly."""
        # All good factors should give reasonable rate
        factor = rewards.voice_factor(
            presence=1.0, company=1.0, freshness=1.0
        )
        self.assertGreater(factor, 0.0)
        self.assertLessEqual(factor, rewards.VC_MAX_FACTOR)
        
        # Any zero factor should result in zero
        factor = rewards.voice_factor(
            presence=0.0, company=1.0, freshness=1.0
        )
        self.assertEqual(factor, 0.0)
        
        factor = rewards.voice_factor(
            presence=1.0, company=0.0, freshness=1.0
        )
        self.assertEqual(factor, 0.0)

    def test_voice_reward_calculation(self):
        """Test that voice rewards are calculated correctly."""
        # Full factor should give configured rates
        payout = rewards.voice_reward(
            factor=1.0, vc_xp=3, vc_coins=2
        )
        self.assertEqual(payout.xp, 3)
        self.assertEqual(payout.coins, 2)
        
        # Half factor should give half rewards (rounded)
        payout = rewards.voice_reward(
            factor=0.5, vc_xp=3, vc_coins=2
        )
        self.assertGreaterEqual(payout.xp, 1)
        self.assertGreaterEqual(payout.coins, 1)
        
        # Zero factor should give nothing
        payout = rewards.voice_reward(
            factor=0.0, vc_xp=3, vc_coins=2
        )
        self.assertEqual(payout.xp, 0)
        self.assertEqual(payout.coins, 0)
        
        # Minimum payout should be at least 1
        payout = rewards.voice_reward(
            factor=0.1, vc_xp=3, vc_coins=2
        )
        self.assertGreaterEqual(payout.xp, 1)
        self.assertGreaterEqual(payout.coins, 1)


if __name__ == "__main__":
    unittest.main()