#!/usr/bin/env python3

__author__ = "Visual Studio"
__description__ = "A script to extract and build shadowboots"
__platforms__ = ["Windows"]
__thanks__ = ["tydye81", "c0z", "golden"]

import re
from io import BytesIO
from json import loads
from pathlib import Path
from binascii import crc32
from ctypes import sizeof, c_ubyte
from argparse import ArgumentParser
from struct import pack, pack_into, unpack_from
from os.path import abspath, isdir, isfile, join

from XeCrypt import *
from StreamIO import *
from build_lib import *

# constants
BIN_DIR = "bin"
XELL_DIR = "XeLL"
BUILD_DIR = Path("Build")
PATCH_DIR = "Patches"
BUILD_VER = 17559
MANIFEST_FILE = "manifest.json"
BUFFER_SIZE = 4096
NOP = 0x60000000

# size defines
HYPERVISOR_SIZE = 0x40000
SMC_CONFIG_SIZE = 280
SHADOWBOOT_SIZE = 0xD4000

# keys
ONE_BL_KEY = None
SB_PRV_KEY = None

def checksum_file(filename: str) -> int:
	cksm = 0
	with open(filename, "rb") as f:
		while b := f.read(BUFFER_SIZE):
			cksm = crc32(b, cksm)
	return cksm

def verify_checksum(filename: str, cksm: int) -> bool:
	if cksm == 0 or not isfile(filename):
		return True
	return checksum_file(filename) == cksm

def pad_hex(i: int) -> str:
	h = pack(">I", i).hex()
	while len(h) < 8:
		h = "0" + h
	return h

def path_type(parser: ArgumentParser, value: str) -> Path:
	return Path(value)

def unecc(path: str, block_size: int = 512, spare_size: int = 16) -> bytes:
	with BytesIO() as bio, open(path, "rb") as f:
		f.seek(0, 2)
		size = f.tell()
		f.seek(0)
		while f.tell() < size:
			data = f.read(block_size)
			f.seek(spare_size, 1)
			bio.write(data)
		return bio.getvalue()

class ShadowbootImage:
	# I/O stream
	_stream = None

	# bootloader header map
	img_map = {}

	# headers
	nand_header = None

	# shadowboot data (all decrypted)
	smc_data = None
	smc_config_data = None
	kv_data = None
	sb_data = None
	sc_data = None
	sd_data = None
	se_data = None
	kernel_data = None
	hypervisor_data = None
	patches = []

	# metadata
	# SB
	sb_sig = None
	sb_pub_key = None
	sc_nonce = None
	sc_salt = None
	sd_salt = None
	sd_digest = None
	# SC
	sc_sig = None
	# SD
	sd_sig = None
	sd_pub_key = None
	sf_nonce = None
	sf_salt = None
	se_digest = None
	# bootloaders
	sb_build = None
	sc_build = None
	sd_build = None
	se_build = None
	# kernel
	kernel_version = None
	# HV
	hypervisor_version = None
	# SMC
	console_type = None
	smc_version = None
	# misc.
	is_retal = False
	is_testkit = False

	def __init__(self) -> None:
		self.reset()

	@staticmethod
	def parse(data: (bytes, bytearray), checks: bool = True, patches: bool = True):
		img = ShadowbootImage()

		with BytesIO(data) as img._stream:
			img.nand_header = img.read_header(NAND_HEADER)

			img.map_shadowboot()

			img.parse_smc()
			# img.parse_smc_config()
			img.parse_keyvault()

			img.parse_sb_2bl()

			img.parse_sc_3bl()

			img.parse_sd_4bl()

			img.parse_se_5bl()
			img.decompress_se_5bl()

			img.parse_metadata()

			if patches:
				img.parse_patches()

			if checks:
				if not img.check_signature_sb_2bl():
					raise Exception("Invalid SB signature")
				if not img.check_signature_sc_3bl():
					raise Exception("Invalid SC signature")
				if not img.check_signature_sd_4bl():
					raise Exception("Invalid SD signature")
				if not img.check_hash_sd_4bl():
					raise Exception("Invalid SD digest")
				if not img.check_hash_se_5bl():
					raise Exception("Invalid SE digest")
			
		return img

	@staticmethod
	def create():
		# probably never going to actually implement this since I have building working already

		new_sb_nonce = XeCryptRandom(0x10)
		new_sb_key = XeCryptHmacSha(XECRYPT_1BL_KEY, new_sb_nonce)[:0x10]

		new_sc_nonce = XeCryptRandom(0x10)
		new_sc_key = XeCryptHmacSha((b"\x00" * 0x10), new_sc_nonce)[:0x10]

		new_sd_nonce = XeCryptRandom(0x10)
		new_sd_key = XeCryptHmacSha(new_sc_key, new_sd_nonce)[:0x10]

		new_se_nonce = XeCryptRandom(0x10)
		new_se_key = XeCryptHmacSha(new_sd_key, new_se_nonce)[:0x10]

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb) -> None:
		pass

	def reset(self) -> None:
		self._stream = None
		self.img_map = {}
		self.nand_header = None
		self.smc_data = None
		self.smc_config_data = None
		self.kv_data = None
		self.sb_data = None
		self.sc_data = None
		self.sd_data = None
		self.se_data = None
		self.kernel_data = None
		self.hypervisor_data = None
		self.patches = []
		self.sb_sig = None
		self.sb_pub_key = None
		self.sc_nonce = None
		self.sc_salt = None
		self.sd_salt = None
		self.sd_digest = None
		self.sc_sig = None
		self.sd_sig = None
		self.sd_pub_key = None
		self.sf_nonce = None
		self.sf_salt = None
		self.se_digest = None
		self.console_type = None
		self.smc_version = None
		self.sb_build = None
		self.sc_build = None
		self.sd_build = None
		self.se_build = None
		self.kernel_version = None
		self.hypervisor_version = None
		self.is_retail = False
		self.is_testkit = False

	def read_header(self, header_type):
		return header_type.from_buffer_copy(self._stream.read(sizeof(header_type)))

	def map_shadowboot(self) -> None:
		self.img_map["SMC"] = {"offset": self.nand_header.smc_offset, "size": self.nand_header.smc_length}
		self.img_map["KV"] = {"offset": self.nand_header.kv_offset, "size": self.nand_header.kv_length}
		self._stream.seek(self.nand_header.cb_offset)
		for i in range(4):
			# all of them are the same
			hdr = BLHeader(self._stream.read(0x20))
			bl_name = hdr.magic.decode("UTF8")
			self.img_map[bl_name] = {"offset": self._stream.tell() - hdr.header_size, "size": hdr.size + 0xF & 0xFFFFFFF0, "header": hdr}
			# derive keys
			if bl_name == "SB":
				self.img_map[bl_name]["key"] = XeCryptHmacSha(XECRYPT_1BL_KEY, hdr.nonce)[:0x10]
				self.img_map[bl_name]["header"]["nonce"] = (b"\x00" * 0x10)
			elif bl_name == "SC":
				self.img_map[bl_name]["key"] = XeCryptHmacSha((b"\x00" * 0x10), hdr.nonce)[:0x10]
				self.img_map[bl_name]["header"]["nonce"] = (b"\x00" * 0x10)
			elif bl_name == "SD":
				self.img_map[bl_name]["key"] = XeCryptHmacSha(self.img_map["SC"]["key"], hdr.nonce)[:0x10]
				self.img_map[bl_name]["header"]["nonce"] = (b"\x00" * 0x10)
			elif bl_name == "SE":
				self.img_map[bl_name]["key"] = XeCryptHmacSha(self.img_map["SD"]["key"], hdr.nonce)[:0x10]
				self.img_map[bl_name]["header"]["nonce"] = (b"\x00" * 0x10)
			# don't seek for the last entry
			if i != 3:
				self._stream.seek(hdr.size - 0x20, 1)

	def parse_smc(self) -> None:
		if self.img_map["SMC"]["offset"] > 0:
			self._stream.seek(self.img_map["SMC"]["offset"])
			self.smc_data = XeCryptSmcDecrypt(self._stream.read(self.img_map["SMC"]["size"]))

	def parse_smc_config(self) -> None:
		if self.nand_header.smc_config_offset > 0:
			self._stream.seek(self.nand_header.smc_config_offset)
			self.smc_config_data = self._stream.read(SMC_CONFIG_SIZE)

	def parse_keyvault(self) -> None:
		if self.img_map["KV"]["offset"] > 0:
			self._stream.seek(self.img_map["KV"]["offset"])
			self.kv_data = self._stream.read(self.img_map["KV"]["size"])

	def parse_sb_2bl(self) -> None:
		# seek to the CB/SB/2BL start
		self._stream.seek(self.img_map["SB"]["offset"] + 0x20)
		# 16-byte alignment
		# self.img_map["SB"]["pad_size"] = (self.img_map["SB"]["size"] + 0xF) & ~0xF
		# read out the encrypted bytes after the header
		sb_2bl_enc = self._stream.read(self.img_map["SB"]["size"] - 0x20)
		# decrypt the CB/SB/2BL
		sb_2bl_dec = XeCryptRc4.new(self.img_map["SB"]["key"]).decrypt(sb_2bl_enc)
		# prepend the header to the decrypted data
		self.sb_data = bytes(self.img_map["SB"]["header"]) + sb_2bl_dec # + (b"\x00" * (self.img_map["SB"]["pad_size"] - self.img_map["SB"]["size"]))

	def parse_sc_3bl(self) -> None:
		# seek to the CC/SC/3BL start
		self._stream.seek(self.img_map["SC"]["offset"] + 0x20)
		# 16-byte alignment
		# self.img_map["SC"]["pad_size"] = (self.img_map["SC"]["size"] + 0xF) & ~0xF
		# read out the encrypted bytes after the header
		sc_3bl_enc = self._stream.read(self.img_map["SC"]["size"] - 0x20)
		# decrypt the CC/SC/3BL
		sc_3bl_dec = XeCryptRc4.new(self.img_map["SC"]["key"]).decrypt(sc_3bl_enc)
		# prepend the header to the decrypted data
		self.sc_data = bytes(self.img_map["SC"]["header"]) + sc_3bl_dec # + (b"\x00" * (self.img_map["SC"]["pad_size"] - self.img_map["SC"]["size"]))

	def parse_sd_4bl(self) -> None:
		# seek to the CD/SD/4BL start
		self._stream.seek(self.img_map["SD"]["offset"] + 0x20)
		# 16-byte alignment
		# self.img_map["SD"]["pad_size"] = (self.img_map["SD"]["size"] + 0xF) & ~0xF
		# read out the encrypted bytes after the header
		sd_4bl_enc = self._stream.read(self.img_map["SD"]["size"] - 0x20)
		# decrypt the CD/SD/4BL
		sd_4bl_dec = XeCryptRc4.new(self.img_map["SD"]["key"]).decrypt(sd_4bl_enc)
		# prepend the header to the decrypted data
		self.sd_data = bytes(self.img_map["SD"]["header"]) + sd_4bl_dec # + (b"\x00" * (self.img_map["SD"]["pad_size"] - self.img_map["SD"]["size"]))

	def parse_se_5bl(self) -> None:
		# seek to the CE/SE/5BL start
		self._stream.seek(self.img_map["SE"]["offset"] + 0x20)
		# 16-byte alignment
		# self.img_map["SE"]["pad_size"] = (self.img_map["SE"]["size"] + 0xF) & ~0xF
		# read out the encrypted bytes after the header
		se_5bl_enc = self._stream.read(self.img_map["SE"]["size"] - 0x20)
		# decrypt the CE/SE/5BL
		se_5bl_dec = XeCryptRc4.new(self.img_map["SE"]["key"]).decrypt(se_5bl_enc)
		# prepend the header to the decrypted data
		self.se_data = bytes(self.img_map["SE"]["header"]) + se_5bl_dec # + (b"\x00" * (self.img_map["SE"]["pad_size"] - self.img_map["SE"]["size"]))

	def decompress_se_5bl(self) -> None:
		data = decompress_se(self.se_data)
		self.hypervisor_data = data[:HYPERVISOR_SIZE]
		self.kernel_data = data[HYPERVISOR_SIZE:]

	def parse_hypervisor(self) -> None:
		with BytesIO(self.hypervisor_data) as bio:
			bio.seek(0x105B8)  # dev PIRS public key
			bio.seek(0x106C8)  # expansion public key
			bio.seek(0x10A18)  # XMACS public key
			bio.seek(0x11008)  # master public key
			bio.seek(0x11988)  # retail PIRS public key

	def parse_patches(self) -> None:
		bl_end = bytes.fromhex("4BFFFFB0000000000000000000000000")
		end_loc = self.sd_data.find(bl_end) + len(bl_end)
		if end_loc == -1 or end_loc == len(self.sd_data):  # no patches
			return
		#bl_end = bytes.fromhex("386000004E8000200000000000000000")
		#end_loc = self.sd_data.find(bl_end) + len(bl_end)
		#if end_loc == -1 or end_loc == len(self.sd_data):  # no patches
		#	return
		# if not self.sd_data.endswith(bytes.fromhex("FFFFFFFF")):  # patches not available
		# 	return

		print("Patches found!")
		with StreamIO(self.sd_data, Endian.BIG) as sio:
			sio.seek(end_loc)  # not static by any means
			patch_loader = sio.read_ubytes(0x40)  # the loader code for patches
			self.patches.append({"offset": end_loc, "size_bytes": len(patch_loader), "patch_loader": patch_loader})
			while True:
				offset = sio.tell()
				address = sio.read_uint32()
				if address == 0xFFFFFFFF:  # end of patches
					break
				size_int32 = sio.read_uint32()
				size_bytes = size_int32 * 4
				patch_code = sio.read_ubytes(size_bytes)
				whole_patch = pack(">2I", address, size_int32) + patch_code
				self.patches.append({"offset": offset, "address": address, "size_int32": size_int32, "size_bytes": (size_bytes * 4), "patch_code": patch_code, "whole_patch": whole_patch})

	def parse_metadata(self) -> None:
		# SB
		self.sb_sig = self.sb_data[64:64 + 256]
		self.sb_pub_key = self.sb_data[616:616 + 272]  # verifies SC and SD
		self.sc_nonce = self.sb_data[888:888 + 0x10]
		self.sc_salt = self.sb_data[904:904 + 0xA]
		self.sd_salt = self.sb_data[914:914 + 0xA]
		self.sd_digest = self.sb_data[924:924 + 0x14]
		# SC
		self.sc_sig = self.sc_data[32:32 + 256]
		# SD
		self.sd_sig = self.sd_data[32:32 + 256]
		self.sd_pub_key = self.sd_data[288:288 + 272]  # verifies SE
		self.sf_nonce = self.sd_data[560:560 + 0x10]
		self.sf_salt = self.sd_data[576:576 + 0xA]
		self.se_digest = self.sd_data[588:588 + 0x14]
		# SMC and kernel
		if self.smc_data is not None:
			num = self.smc_data[256]
			self.console_type = [
				"Error",
				"Xenon",
				"Zephyr",
				"Falcon",
				"Jasper",
				"Trinity",
				"Corona",
				"Winchester",
				"Unknown",
				"Unknown"
			][num >> 4 & 15]
			self.smc_version = f"{num >> 4 & 15}.{num & 15} ({self.smc_data[257]}.{self.smc_data[258]})"
		# (self.kernel_version,) = unpack_from(">H", self.kernel_data, 0x40C)
		self.sb_build = self.img_map["SB"]["header"]["build"]
		self.sc_build = self.img_map["SC"]["header"]["build"]
		self.sd_build = self.img_map["SD"]["header"]["build"]
		self.se_build = self.img_map["SE"]["header"]["build"]
		(self.hypervisor_version,) = unpack_from(">H", self.hypervisor_data, 0x2)
		self.kernel_version = self.se_build
		self.is_retail = self.hypervisor_data[0] == 0x4E
		self.is_testkit = bytes.fromhex("5C746573746B69745C") in self.kernel_data

	def check_signature_sb_2bl(self) -> bool:
		sb_hash = XeCryptRotSumSha(self.sb_data[:0x10] + self.sb_data[0x140:])  # skips the nonce and signature
		assert len(ONE_BL_KEY) == XECRYPT_RSAPUB_2048_SIZE, "Invalid 1BL public key size"
		return XeCryptBnQwBeSigVerify(self.sb_sig, sb_hash, XECRYPT_1BL_SALT, ONE_BL_KEY)

	def check_signature_sc_3bl(self) -> bool:
		sc_hash = XeCryptRotSumSha(self.sc_data[:0x10] + self.sc_data[0x120:])  # skips the nonce and signature
		assert len(self.sb_pub_key) == XECRYPT_RSAPUB_2048_SIZE, "Invalid SB public key size"
		return XeCryptBnQwBeSigVerify(self.sc_sig, sc_hash, self.sc_salt, self.sb_pub_key)

	def check_signature_sd_4bl(self) -> bool:
		sd_hash = XeCryptRotSumSha(self.sd_data[:0x10] + self.sd_data[0x120:])  # skips the nonce and signature
		assert len(self.sb_pub_key) == XECRYPT_RSAPUB_2048_SIZE, "Invalid SB public key size"
		return XeCryptBnQwBeSigVerify(self.sd_sig, sd_hash, self.sd_salt, self.sb_pub_key)

	def check_hash_sd_4bl(self) -> bool:
		if self.sd_digest != b"\x00" * len(self.sd_digest):
			return XeCryptRotSumSha(self.sd_data[:0x10] + self.sd_data[0x20:]) == self.sd_digest
		return True

	def check_hash_se_5bl(self) -> bool:
		if self.se_digest != b"\x00" * len(self.se_digest):
			return XeCryptRotSumSha(self.se_data[:0x10] + self.se_data[0x20:]) == self.se_digest
		return True

	def print_info(self) -> None:
		print(f"Console Type:   {self.console_type}")
		print(f"SMC Version:    {self.smc_version}")
		print(f"SB Version:     {self.sb_build}")
		print(f"SC Version:     {self.sc_build}")
		print(f"SD Version:     {self.sd_build}")
		print(f"SE Version:     {self.se_build}")
		print(f"HV Version:     {self.hypervisor_version}")
		print(f"Kernel Version: {self.kernel_version}")

		if self.is_retail:
			print("Main Menu:      Dashboard")
		else:
			print("Main Menu:      XShell")

		if self.is_testkit:
			print("Hardware:       Test Kit")
		else:
			print("Hardware:       Development Kit")

def main() -> None:
	global MANIFEST_FILE, ONE_BL_KEY, SB_PRV_KEY

	parser = ArgumentParser(description=__description__)
	subparsers = parser.add_subparsers(dest="command")

	build_parser = subparsers.add_parser("build")
	# build_parser.add_argument("input", type=str, help="The input path")
	build_parser.add_argument("output", type=lambda x: path_type(build_parser, x), help="The output path")
	build_parser.add_argument("--nochecks", action="store_true", help="Perform shadowboot parsing without integrity checks")
	build_parser.add_argument("-m", "--manifest", type=lambda x: path_type(build_parser, x), help="The build manifest file")
	build_parser.add_argument("-b", "--build-dir", type=lambda x: path_type(build_parser, x), help="The build directory path")

	extract_parser = subparsers.add_parser("extract")
	extract_parser.add_argument("input", type=lambda x: path_type(extract_parser, x), help="The input path")
	extract_parser.add_argument("output", type=lambda x: path_type(extract_parser, x), help="The output path")
	# extract_parser.add_argument("--nochecks", action="store_true", help="Extract without doing sanity checks")
	# extract_parser.add_argument("--raw", action="store_true", help="No decryption performed")
	extract_parser.add_argument("--flash", action="store_true", help="Parse a flash image instead of a shadowboot")
	extract_parser.add_argument("--nochecks", action="store_true", help="Perform shadowboot parsing without integrity checks")
	extract_parser.add_argument("--all", action="store_true", help="Extract all sections")
	extract_parser.add_argument("--smc", action="store_true", help="Extract the SMC")
	extract_parser.add_argument("--keyvault", "--kv", action="store_true", help="Extract the keyvault")
	extract_parser.add_argument("--sb", action="store_true", help="Extract the SB")
	extract_parser.add_argument("--sc", action="store_true", help="Extract the SC")
	extract_parser.add_argument("--sd", action="store_true", help="Extract the SD")
	extract_parser.add_argument("--se", action="store_true", help="Extract the SE")
	extract_parser.add_argument("--kernel", action="store_true", help="Extract the kernel")
	extract_parser.add_argument("--hypervisor", "--hv", action="store_true", help="Extract the hypervisor")
	extract_parser.add_argument("--loader", action="store_true", help="Extract HV/kernel patch loader")
	extract_parser.add_argument("--patches", action="store_true", help="Extract HV/kernel patches")

	info_parser = subparsers.add_parser("info")
	info_parser.add_argument("input", type=lambda x: path_type(info_parser, x), help="The input path")
	info_parser.add_argument("--flash", action="store_true", help="Parse a flash image instead of a shadowboot")
	info_parser.add_argument("--nochecks", action="store_true", help="Perform shadowboot parsing without integrity checks")

	split_parser = subparsers.add_parser("split")
	split_parser.add_argument("input", type=lambda x: path_type(info_parser, x), help="The input path")
	split_parser.add_argument("output", type=lambda x: path_type(extract_parser, x), help="The output path")
	split_parser.add_argument("--nochecks", action="store_true", help="Perform shadowboot parsing without integrity checks")

	test_parser = subparsers.add_parser("test")
	test_parser.add_argument("input", type=lambda x: path_type(test_parser, x), help="The input path")
	test_parser.add_argument("output", type=lambda x: path_type(test_parser, x), help="The output path")
	test_parser.add_argument("--nochecks", action="store_true", help="Perform shadowboot parsing without integrity checks")

	args = parser.parse_args()

	# the 1BL public key
	ONE_BL_KEY = read_file("Keys/1BL_pub.bin")
	assert crc32(ONE_BL_KEY) == 0xD416B5E1, "Invalid 1BL public key"

	# this used to sign the SD and it's public key is in SB
	SB_PRV_KEY = read_file("Keys/SB_prv.bin")
	assert crc32(SB_PRV_KEY) == 0x490C9D35, "Invalid SD private key"

	if args.command == "build":
		if args.manifest.is_file():  # building with a manifest file
			# load the manifest file
			print("Loading build manifest...")
			build_manifest = loads(args.manifest.read_text())

			# remove comments
			del build_manifest["_comment"]
			del build_manifest["build"]["_comment"]
			del build_manifest["options"]["_comment"]
			del build_manifest["files"]["_comment"]

			# settings
			# test_kit_compile = build_manifest["options"]["test_kit"]
			# sd_code_enabled = build_manifest["options"]["SD_code_enabled"]
			# sd_patches_enabled = build_manifest["options"]["SD_patches_enabled"]

			# paths
			print("Setting up paths...")
			bd = Path(build_manifest["files"]["base_directory"])
			base_img_file = Path(build_manifest["files"]["base_image"])
			smc_bin_file = bd /  build_manifest["files"]["SMC"]
			smc_cfg_file = bd /  build_manifest["files"]["SMC_config"]
			kv_file = bd / build_manifest["files"]["KV"]
			sb_file = bd / build_manifest["files"]["SB"]
			sc_file = bd / build_manifest["files"]["SC"]
			sd_file = bd / build_manifest["files"]["SD"]
			se_file = bd / build_manifest["files"]["SE"]
			kernel_file = bd / build_manifest["files"]["kernel"]
			hypervisor_file = bd / build_manifest["files"]["HV"]
			sd_patches_file = bd / build_manifest["files"]["SD_patches"]
			sd_code_file = bd / build_manifest["files"]["SD_code"]
			hvk_patches_file = bd / build_manifest["files"]["HVK_patches"]
			# patch_loader_file = join("bin/loaders", "patch_loader.bin")

			# check build manifest files

			"""
			print("Verifying checksums...")

			all_files_available = True
			for (key, value) in build_manifest["files"].items():
				if not key.endswith("_checksum"):
					if value != "":
						if key == "base_image":
							if not isfile(value):
								all_files_available = False
						elif key == "base_directory":
							if not (Path(BUILD_DIR) / value).is_dir():
								all_files_available = False
						else:
							if not (bd / value).is_file():
								all_files_available = False
				else:
					assert verify_checksum(build_manifest["files"][key.replace("_checksum", "")], value), f"Invalid {key}"
			"""
		elif args.build_dir is not None:  # using a build directory vs a manifest
			smc_bin_file = args.build_dir / "SMC_dec.bin"
			smc_cfg_file = args.build_dir / "SMC_config.bin"
			kv_file = args.build_dir / "KV_dec.bin"
			sb_file = args.build_dir / "SB.bin"
			sc_file = args.build_dir / "SC.bin"
			sd_file = args.build_dir / "SD.bin"
			se_file = args.build_dir / "SE.bin"
			kernel_file = args.build_dir / "kernel.bin"
			hypervisor_file = args.build_dir / "hypervisor.bin"
			base_img_file = args.build_dir / "xboxrom.bin"

			# patches
			sd_code_file = args.build_dir / "sdc.bin"  # SD code file
			sd_patches_file = args.build_dir / "sdp.bin"  # SD patches file
			hvk_patches_file = args.build_dir / "hvk.bin"
		else:
			print("Building requires -m or -b arguments!")
			return

		# check for the base image and load it if it exists
		base_img = None
		base_img_available = base_img_file.is_file()
		if base_img_available:
			base_img = ShadowbootImage.parse(base_img_file.read_bytes(), not build_manifest["options"]["base_image_checks_disabled"])

		if sb_file.is_file():
			print("Reading SB from file...")
			sb_data = sb_file.read_bytes()
		elif base_img_available:
			print("Reading SB from base image...")
			sb_data = base_img.sb_data
		else:
			raise Exception("No SB binary or fallback image was provided!")

		if sc_file.is_file():
			print("Reading SC from file...")
			sc_data = sc_file.read_bytes()
		elif base_img_available:
			print("Reading SC from base image...")
			sc_data = base_img.sc_data
		else:
			raise Exception("No SC binary or fallback image was provided!")

		if sd_file.is_file():
			print("Reading SD from file...")
			sd_data = sd_file.read_bytes()
		elif base_img_available:
			print("Reading SD from base image...")
			sd_data = base_img.sd_data
		else:
			raise Exception("No SD binary or fallback image was provided!")

		if kernel_file.is_file() and hypervisor_file.is_file():
			print("Reading raw HV/kernel...")
			kernel = kernel_file.read_bytes()
			hypervisor = hypervisor_file.read_bytes()
			se_data = hypervisor + kernel
		elif se_file.is_file():
			print("Decompressing SE...")
			se_data = decompress_se(se_file.read_bytes())
		elif base_img_available:
			print("Using base image HV/kernel...")
			se_data = base_img.hypervisor_data + base_img.kernel_data
		else:
			raise Exception("No HV/kernel pair, SE, or fallback image was provided!")

		print("Applying patches to HV and kernel...")
		se_data = apply_patches(se_data, hvk_patches_file.read_bytes())

		# generate new nonce's and keys
		print("Generating new nonce's and encryption keys...")
		new_sb_nonce = XeCryptRandom(0x10)
		new_sb_key = XeCryptHmacSha(XECRYPT_1BL_KEY, new_sb_nonce)[:0x10]

		new_sc_nonce = XeCryptRandom(0x10)
		new_sc_key = XeCryptHmacSha((b"\x00" * 0x10), new_sc_nonce)[:0x10]

		new_sd_nonce = XeCryptRandom(0x10)
		new_sd_key = XeCryptHmacSha(new_sc_key, new_sd_nonce)[:0x10]

		new_se_nonce = XeCryptRandom(0x10)
		new_se_key = XeCryptHmacSha(new_sd_key, new_se_nonce)[:0x10]

		# set header values
		print("Setting initial NAND header values...")
		if build_manifest["build"]["copyright"]:
			print("Using custom copyright...")
			copyright = b"\xA9 " + build_manifest["build"]["copyright"].encode("UTF8")
		else:
			copyright = b"\xA9 2005-2022 Microsoft Corporation. All rights reserved"

		if build_manifest["build"]["version"] > 0:
			print("Using custom build version...")
			build_ver = build_manifest["build"]["version"]
		else:
			build_ver = unpack_from(">H", se_data, HYPERVISOR_SIZE + 0x40C)[0]

		# create NAND header
		nand_header = NAND_HEADER()
		nand_header.magic = 0xFF4F
		nand_header.build = build_ver
		nand_header.qfe = 0x8000
		nand_header.copyright = (c_ubyte * 0x40)(*copyright)

		# create room for the NAND header
		print("Creating empty NAND header...")
		new_img = bytearray(sizeof(nand_header))

		# SMC
		smc_offset = len(new_img)
		if build_manifest["options"]["use_smc"]:
			#if (BUILD_DIR / "SMC_dec.bin").is_file():
			print(f"Encrypting and writing SMC_dec.bin @ 0x{smc_offset:04X}...")
			nand_header.smc_offset = sizeof(nand_header)  # right after NAND header
			smc_data = XeCryptSmcEncrypt(Path(smc_bin_file).read_bytes())
			nand_header.smc_length = len(smc_data)
			new_img += smc_data
			#elif (BUILD_DIR / "SMC_enc.bin").is_file():
			#	print(f"Writing encrypted SMC_enc.bin @ 0x{smc_offset:04X}...")
			#	nand_header.smc_offset = sizeof(nand_header)  # right after NAND header
			#	smc_data = (BUILD_DIR / "SMC_enc.bin").read_bytes()
			#	nand_header.smc_length = len(smc_data)
			#	new_img += smc_data
			#else:
			#	print("SMC_dec.bin and SMC_enc.bin not found, skipping...")

		# KeyVault (no idea if it even loads it)
		kv_offset = len(new_img)
		if build_manifest["options"]["use_kv"]:
			#if (BUILD_DIR / "KV_dec.bin").is_file():
			if kv_file.name.lower().endswith("_dec.bin"):
				nand_header.kv_offset = kv_offset
				cpu_key = bytes.fromhex(build_manifest["build"]["cpu_key"])
				print(f"Encrypting and writing KV_dec.bin @ 0x{kv_offset:04X}...")
				kv_data = XeCryptKeyVaultEncrypt(cpu_key, kv_file.read_bytes())
				nand_header.kv_length = len(kv_data)
				new_img += kv_data
			elif kv_file.name.lower().endswith("_enc.bin"):
				nand_header.kv_offset = kv_offset
				print(f"Writing KV_enc.bin @ 0x{kv_offset:04X}...")
				kv_data = kv_file.read_bytes()
				nand_header.kv_length = len(kv_data)
				new_img += kv_data
			#elif (BUILD_DIR / "KV_enc.bin").is_file():
			#	print(f"Writing encrypted KV_enc.bin @ 0x{kv_offset:04X}...")
			#	nand_header.kv_offset = kv_offset
			#	kv_data = (BUILD_DIR / "KV_enc.bin").read_bytes()
			#	nand_header.kv_length = len(kv_data)
			#	new_img += kv_data
			#else:
			#	print("KV_dec.bin and KV_enc.bin not found, skipping...")

		# write SB
		sb_offset = len(new_img)
		nand_header.cb_offset = sb_offset
		print(f"Encrypting and writing SB @ 0x{sb_offset:04X}...")
		nonce_sb = bytearray(sb_data)
		pack_into("<16s", nonce_sb, 0x10, new_sb_nonce)

		if build_manifest["options"]["test_kit"]:
			print("Compiling for test kit, SB signature will be broken!")
			assert nonce_sb[0x1348:0x1348 + 4] == bytes.fromhex("419A0014"), "Original bytes mismatch!"
			pack_into("<4s", nonce_sb, 0x1348, bytes.fromhex("48000194"))

		sb_enc = encrypt_bl(new_sb_key, nonce_sb)
		new_img += sb_enc
		sc_offset = len(new_img)

		# write SC
		print(f"Encrypting and writing SC @ 0x{sc_offset:04X}...")
		nonce_sc = bytearray(sc_data)
		pack_into("<16s", nonce_sc, 0x10, new_sc_nonce)
		sc_enc = encrypt_bl(new_sc_key, nonce_sc)
		new_img += sc_enc
		sd_offset = len(new_img)

		# create SE image
		print("Creating SE...")
		se_dec = se_data
		print("Compressing SE...")
		se_com = compress_se(se_dec)
		# magic, build, QFE, flags, and entry point
		pack_into(">2s 3H I", se_com, 0, b"SE", build_ver, 0x8000, 0, 0)
		# write the nonce into the image
		pack_into("<16s", se_com, 0x10, new_se_nonce)

		assert len(se_com) == unpack_from(">I", se_com, 12)[0], "Invalid SE size"
		se_com += (b"\x00" * calc_bldr_pad_size(len(se_com)))

		print("Hashing SE...")
		se_hash = XeCryptRotSumSha(se_com[:0x10] + se_com[0x20:])
		print("Encrypting SE...")
		se_enc = encrypt_bl(new_se_key, se_com)

		# write SD
		print(f"Signing, encrypting, and writing SD @ 0x{sd_offset:04X}...")
		nonce_sd = bytearray(sd_data)
		pack_into("<16s", nonce_sd, 0x10, new_sd_nonce)
		pack_into("<20s", nonce_sd, 0x24C, se_hash)
		sd_patched = nonce_sd
		# load additional binary data to run after the SD here
		if sd_code_file.is_file():
			print("Patching RFID jump and appending SD code binary...")
			sd_patched = apply_jump_sd_4bl(sd_patched, unpack_from(">I", sd_patched, 0xC)[0])
			# sd_patched += read_file(patch_loader_file)
			sd_patched += sd_code_file.read_bytes()
		# apply SD patches directly
		if sd_patches_file.is_file():
			print("Applying SD patches directly...")
			sd_patched = apply_patches(sd_patched, sd_patches_file.read_bytes())
		# apply padding
		# sd_patched += (b"\x00" * calc_pad_size(len(sd_patched)))
		# pack_into(">I", sd_patched, 0xC, len(sd_patched))  # set the new size
		if verify_sd_4bl(SB_PRV_KEY, XECRYPT_SD_SALT, sd_patched):
			print("SD signature is intact, skipping signing...")
			sd_res = sd_patched
		else:
			sd_res = sign_sd_4bl(SB_PRV_KEY, XECRYPT_SD_SALT, sd_patched)
		sd_enc = encrypt_bl(new_sd_key, sd_res)
		new_img += sd_enc
		se_offset = len(new_img)

		# write SE
		print(f"Writing SE to 0x{se_offset:04X}...")
		new_img += se_enc

		# write NAND header
		print("Writing NAND header @ 0x0...")
		pack_into(f"<{sizeof(nand_header)}s", new_img, 0, bytes(nand_header))

		# padding image to 0x1000
		# new_img += (b"\x00" * (0xD0000 - len(new_img)))
		new_img += (b"\x00" * calc_pad_size(len(new_img), 0x1000))

		# write the output image
		print("Writing output image...")
		args.output.write_bytes(new_img)
		img_size = len(new_img)

		print(f"Image size: {img_size}/{SHADOWBOOT_SIZE} (0x{img_size:04X}/0x{SHADOWBOOT_SIZE:04X}) bytes")
		if len(new_img) < SHADOWBOOT_SIZE:
			red_size = SHADOWBOOT_SIZE - len(new_img)
			print(f"Image reduced by {red_size} (0x{red_size:04X}) bytes!")

		if not build_manifest["options"]["test_kit"]:
			ShadowbootImage.parse(new_img)
			print("Modified image verified!")
		else:
			print("Image is test kit compiled, verification is disabled!")

		print(f"Final image location: \"{str(args.output.absolute())}\"")
	elif args.command == "extract":
		if args.flash:
			img = ShadowbootImage.parse(unecc(str(args.input)), not args.nochecks)
		else:
			img = ShadowbootImage.parse(args.input.read_bytes(), not args.nochecks)

		img.print_info()

		if args.all or args.smc:
			# write_file(join(args.output, "SMC_dec.bin"), img.smc_data)
			if img.smc_data is not None and len(img.smc_data) > 0:
				(args.output / "SMC_dec.bin").write_bytes(img.smc_data)
		# if args.all or args.smc_config:
		# 	(args.output / "smc_config.bin").write_bytes(img.smc_config_data)
		if args.all or args.keyvault:
			if img.kv_data is not None and len(img.kv_data) > 0:
				(args.output / "KV_enc.bin").write_bytes(img.kv_data)
		if args.all or args.sb:
			(args.output / f"SB_{img.sb_build}.bin").write_bytes(img.sb_data)
		if args.all or args.sc:
			(args.output / f"SC_{img.sc_build}.bin").write_bytes(img.sc_data)
		if args.all or args.sd:
			(args.output / f"SD_{img.sd_build}.bin").write_bytes(img.sd_data)
		if args.all or args.se:
			(args.output / f"SE_{img.se_build}.bin").write_bytes(img.se_data)
		if args.all or args.kernel:
			(args.output / "kernel.exe").write_bytes(img.kernel_data)
		if args.all or args.hypervisor:
			(args.output / "hypervisor.bin").write_bytes(img.hypervisor_data)
		if args.all or args.loader:
			if len(img.patches) > 0:
				(args.output / "patch_loader.bin").write_bytes(img.patches[0]["patch_loader"])
			else:
				print("No patch loader found!")
		if args.all or args.patches:
			if len(img.patches) > 1:
				if not (args.output / "Patches").is_dir():
					(args.output / "Patches").mkdir()

				for patch in img.patches[1:]:
					addr = patch["address"]
					if addr > HYPERVISOR_SIZE:
						addr += 0x80000000
					(args.output / "Patches" / f"{addr:08X}.bin").write_bytes(patch["patch_code"])

				combined = b""
				for patch in img.patches[1:]:
					combined += pack(">2I", patch["address"], patch["size_int32"])
					combined += patch["patch_code"]
				combined += (b"\xFF" * 4)

				(args.output / "patches_raw.bin").write_bytes(combined)
			else:
				print("No patches found!")
	elif args.command == "info":
		if args.flash:
			img = ShadowbootImage.parse(unecc(str(args.input)), not args.nochecks)
		else:
			img = ShadowbootImage.parse(args.input.read_bytes(), not args.nochecks)

		img.print_info()
	elif args.command == "split":
		# cabextract -p --filter "KERNEL/*" XDK_0.cab > images.bin

		image_data = args.input.read_bytes()
		IMAGE_EXP = re.compile(rb"Microsoft Corporation\. All rights reserved")
		idxs = [m.start() - 28 for m in IMAGE_EXP.finditer(image_data)]
		for i in range(0, len(idxs)):
			if idxs[i] == idxs[-1]:  # last entry
				data = image_data[idxs[i]:]
			else:  # other entries
				data = image_data[idxs[i]:idxs[i + 1]]
			img = ShadowbootImage.parse(data, not args.nochecks, False)
			if img.is_testkit and not img.is_retail and img.kernel_version == 17489 and img.hypervisor_version == 17489 and img.console_type == "Xenon":
				# Path("Output/Extracted/Test Kit/hypervisor.bin").write_bytes(img.hypervisor_data)
				Path("Output/Extracted/xboxromtw2d.bin").write_bytes(data)
				print("Found!")
				break
			# (args.output / f"xboxrom_update_{i}.bin").write_bytes(data)
	elif args.command == "test":
		pass

if __name__ == "__main__":
	main()
