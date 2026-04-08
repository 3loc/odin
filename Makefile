INVENTORY = ansible/inventory.yml
PLAYBOOK  = ansible/deploy-odin.yml

.PHONY: deploy setup check probe run logs status restart stop

deploy:
	ansible-playbook -i $(INVENTORY) $(PLAYBOOK)

setup:
	ansible-playbook -i $(INVENTORY) $(PLAYBOOK) --ask-become-pass

check:
	ansible-playbook -i $(INVENTORY) $(PLAYBOOK) --check --diff

# Smoke test the running daemon
probe:
	@echo "=== odin.service ==="
	@systemctl is-active odin.service || true
	@systemctl --no-pager --lines=10 status odin.service 2>&1 | head -20 || true
	@echo
	@echo "=== recent journal ==="
	@journalctl -u odin.service -n 15 --no-pager

logs:
	journalctl -u odin.service -f --no-pager

status:
	systemctl status odin.service --no-pager

restart:
	sudo systemctl restart odin.service

stop:
	sudo systemctl stop odin.service
