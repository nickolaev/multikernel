#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <linux/kexec.h>
#include <linux/reboot.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

/* Intentionally mirrors the private struct in kernel/reboot.c on mk-master. */
struct multikernel_boot_args {
	int mk_id;
};

_Static_assert(sizeof(struct multikernel_boot_args) == sizeof(int),
	       "unexpected multikernel reboot argument layout");

static void usage(const char *program)
{
	fprintf(stderr,
		"usage:\n"
		"  %s load ID KERNEL INITRD CMDLINE\n"
		"  %s exec ID\n"
		"  %s halt ID\n"
		"  %s force-halt ID\n",
		program, program, program, program);
}

static int parse_id(const char *text)
{
	char *end = NULL;
	long id;

	errno = 0;
	id = strtol(text, &end, 10);
	if (errno || !end || *end || id < 1 || id > 2047) {
		fprintf(stderr, "mkctl: invalid instance ID: %s\n", text);
		exit(EXIT_FAILURE);
	}
	return (int)id;
}

static void close_checked(int fd, const char *what)
{
	if (close(fd) && errno != EINTR) {
		perror(what);
		exit(EXIT_FAILURE);
	}
}

static int load_instance(int id, const char *kernel, const char *initrd,
			 const char *cmdline)
{
	unsigned long flags = KEXEC_MULTIKERNEL | KEXEC_MK_ID(id);
	int kernel_fd = open(kernel, O_RDONLY | O_CLOEXEC);
	int initrd_fd;
	long rc;

	if (kernel_fd < 0) {
		perror(kernel);
		return EXIT_FAILURE;
	}
	initrd_fd = open(initrd, O_RDONLY | O_CLOEXEC);
	if (initrd_fd < 0) {
		perror(initrd);
		close(kernel_fd);
		return EXIT_FAILURE;
	}

	rc = syscall(SYS_kexec_file_load, kernel_fd, initrd_fd,
		     strlen(cmdline) + 1, cmdline, flags);
	if (rc < 0) {
		perror("kexec_file_load(multikernel)");
		close(kernel_fd);
		close(initrd_fd);
		return EXIT_FAILURE;
	}

	close_checked(kernel_fd, "close kernel");
	close_checked(initrd_fd, "close initrd");
	printf("MK_STAGE_LOAD_SYSCALL_OK id=%d flags=0x%lx\n", id, flags);
	return EXIT_SUCCESS;
}

static int reboot_instance(int id, unsigned int command, const char *marker)
{
	struct multikernel_boot_args args = { .mk_id = id };
	long rc = syscall(SYS_reboot, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
			  command, &args);

	if (rc < 0) {
		perror("reboot(multikernel)");
		return EXIT_FAILURE;
	}
	printf("%s id=%d\n", marker, id);
	return EXIT_SUCCESS;
}

int main(int argc, char **argv)
{
	int id;

	if (argc < 3) {
		usage(argv[0]);
		return EXIT_FAILURE;
	}
	id = parse_id(argv[2]);

	if (!strcmp(argv[1], "load") && argc == 6)
		return load_instance(id, argv[3], argv[4], argv[5]);
	if (!strcmp(argv[1], "exec") && argc == 3)
		return reboot_instance(id, LINUX_REBOOT_CMD_MULTIKERNEL,
				       "MK_STAGE_EXEC_SYSCALL_OK");
	if (!strcmp(argv[1], "halt") && argc == 3)
		return reboot_instance(id, LINUX_REBOOT_CMD_MULTIKERNEL_HALT,
				       "MK_STAGE_HALT_SYSCALL_OK");
	if (!strcmp(argv[1], "force-halt") && argc == 3)
		return reboot_instance(id, LINUX_REBOOT_CMD_MULTIKERNEL_HALT_FORCE,
				       "MK_STAGE_FORCE_HALT_SYSCALL_OK");

	usage(argv[0]);
	return EXIT_FAILURE;
}
