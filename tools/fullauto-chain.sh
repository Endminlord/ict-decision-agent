MK=/home/endmin/dsh/research/import-full.marker
while [ "$(systemctl --user is-active import-corpus 2>/dev/null)" = "active" ]; do sleep 120; done
echo "$(date +%F\ %T) import unit ended, cooling 10 min" >> "$MK"
sleep 600
cd /home/endmin/dsh/research/ict-open-source
echo "$(date +%F\ %T) launching replay-full (self-gated >=757810)" >> "$MK"
if bash tools/replay-full.sh > /tmp/fullauto-replay.log 2>&1; then
  echo "$(date +%F\ %T) replay OK, launching a2-gate" >> "$MK"
  bash tools/a2-gate.sh > /tmp/fullauto-a2.log 2>&1 && echo "$(date +%F\ %T) A2 DONE -- decisions in evidence/a2-gate-*.json" >> "$MK" || echo "$(date +%F\ %T) a2-gate FAILED (see /tmp/fullauto-a2.log)" >> "$MK"
else
  echo "$(date +%F\ %T) replay ABORTED (gate: below 757810 or error) -- see /tmp/fullauto-replay.log" >> "$MK"
fi