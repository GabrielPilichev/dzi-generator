"""LearnPilot unittest suite."""
from tests.seed_guard import install_seed_tripwire

# Record the tracked seed database before any test can touch it; see seed_guard.
install_seed_tripwire()
