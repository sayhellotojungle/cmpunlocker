# SPDX-License-Identifier: GPL-2.0-only
obj-m += cmp_hbm_pm.o

KVER ?= $(shell uname -r)

# pm_runtime_get_if_active() grew a second argument, and distribution kernels
# backport it independently of their version number, so ask the headers this
# build is using instead of guessing from the kernel release.  Same probe the
# source's #ifdef name comes from; a miss means the older one-argument form.
ccflags-y += $(if $(shell grep -s -A 1 'pm_runtime_get_if_active' \
	$(srctree)/include/linux/pm_runtime.h | grep 'bool'), \
	-DCMP_HBM_PM_ACTIVE_HAS_IGNORE)

all:
	$(MAKE) -C /lib/modules/$(KVER)/build M=$(CURDIR) modules

clean:
	$(MAKE) -C /lib/modules/$(KVER)/build M=$(CURDIR) clean
