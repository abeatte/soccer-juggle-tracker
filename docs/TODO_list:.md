# TODO:
1. update setup instructions to create working dirs: ~/mosquitto, ~/frigate, ~/homeassistant, and ~/juggletracker
2. add frigate/inbox-bridge to metrics AND logs in frigate UI
3. need to fix status (processing, unknown, etc) in HA dashboard
4. need to create way to move file from failed to inbox in HA dashboard
5. need to add /srv/juggle_[inbox|processed|failed] to frigate UI storage (maybe in frigate config/docker_compose volumes section?)
6. log `ls -al /srv/juggle_inbox/ /srv/juggle_processed/ /srv/juggle_failed/` as useful to get processing state of the system
7. convert MIGRATION.md into the SOP for deploying new changes (NOTE: some file paths and steps have diverged)
8. video clips from frigate seem to be cut off too short. 