from django.test import TestCase

from waldur_firecrest import scripts, software
from waldur_mastermind.marketplace.tests import factories

ZEN4_MAP = {
    "2023.06": {
        "x86_64/generic": "x86_64/generic",
        "x86_64/amd/zen3": "x86_64/amd/zen3",
        "x86_64/amd/zen4": "x86_64/amd/zen4",
        "x86_64/amd/zen5": "x86_64/amd/zen4",
        "riscv64/generic": "",
    }
}


class SoftwareTestBase(TestCase):
    def setUp(self):
        self.offering = factories.OfferingFactory()
        self.eessi = factories.SoftwareCatalogFactory(
            name="EESSI",
            version="2023.06",
            catalog_type="binary_runtime",
            metadata={"architectures_map": ZEN4_MAP},
        )
        self.link = factories.OfferingSoftwareCatalogFactory(
            offering=self.offering,
            catalog=self.eessi,
            enabled_cpu_family=[],
            enabled_cpu_microarchitectures=[],
        )
        self.gromacs = factories.SoftwarePackageFactory(
            catalog=self.eessi, name="GROMACS"
        )

    def add_version(self, version, *targets, package=None):
        """targets: (full cpu arch, [gpu architectures])"""
        sv = factories.SoftwareVersionFactory(
            package=package or self.gromacs,
            version=version,
            metadata={"module": {"full_module_name": f"GROMACS/{version}"}},
        )
        for arch, gpus in targets:
            family, _, subtype = arch.partition("/")
            factories.SoftwareTargetFactory(
                version=sv,
                target_name=family,
                target_subtype=subtype,
                gpu_architectures=gpus,
            )
        return sv

    def partition(self, cpu_arch="", gpu_arch="", name="batch"):
        return factories.OfferingPartitionFactory(
            offering=self.offering,
            partition_name=name,
            cpu_arch=cpu_arch,
            gpu_arch=gpu_arch,
        )

    def found(self, partition=None, query="gromacs"):
        return sorted(
            m.version
            for m in software.find_software(
                self.offering, query=query, partition=partition
            )
        )


class FindSoftwareTest(SoftwareTestBase):
    def test_provider_microarchitecture_restriction_applies(self):
        self.add_version("1-zen3", ("x86_64/amd/zen3", []))
        self.add_version("2-spr", ("x86_64/intel/sapphirerapids", []))
        self.link.enabled_cpu_microarchitectures = ["amd/zen3"]
        self.link.save()

        self.assertEqual(self.found(), ["1-zen3"])

    def test_restriction_saved_as_short_token_applies(self):
        self.add_version("1-zen3", ("x86_64/amd/zen3", []))
        self.add_version("2-spr", ("x86_64/intel/sapphirerapids", []))
        self.link.enabled_cpu_microarchitectures = ["zen3"]
        self.link.save()

        self.assertEqual(self.found(), ["1-zen3"])

    def test_provider_family_restriction_applies(self):
        self.add_version("1-x86", ("x86_64/generic", []))
        self.add_version("2-arm", ("aarch64/generic", []))
        self.link.enabled_cpu_family = ["aarch64"]
        self.link.save()

        self.assertEqual(self.found(), ["2-arm"])

    def test_partition_offered_only_its_microarchitecture(self):
        self.add_version("1-zen4", ("x86_64/amd/zen4", []))
        self.add_version("2-zen3", ("x86_64/amd/zen3", []))

        self.assertEqual(self.found(self.partition("x86_64/amd/zen4")), ["1-zen4"])

    def test_partition_uses_the_build_eessi_maps_it_to(self):
        self.add_version("1-zen4", ("x86_64/amd/zen4", []))
        self.add_version("2-zen3", ("x86_64/amd/zen3", []))

        self.assertEqual(self.found(self.partition("x86_64/amd/zen5")), ["1-zen4"])

    def test_partition_with_short_microarchitecture(self):
        self.add_version("1-zen4", ("x86_64/amd/zen4", []))
        self.add_version("2-zen3", ("x86_64/amd/zen3", []))

        self.assertEqual(self.found(self.partition("x86_64/zen4")), ["1-zen4"])

    def test_cpu_missing_from_map_matches_the_family(self):
        self.add_version("1-zen4", ("x86_64/amd/zen4", []))
        self.add_version("2-zen3", ("x86_64/amd/zen3", []))
        self.add_version("3-arm", ("aarch64/generic", []))

        partition = self.partition("x86_64/amd/zen6")
        self.assertEqual(self.found(partition), ["1-zen4", "2-zen3"])

    def test_catalog_without_map_matches_the_family(self):
        self.eessi.metadata = {"architectures": ["x86_64/amd/zen4"]}
        self.eessi.save()
        self.add_version("1-zen4", ("x86_64/amd/zen4", []))
        self.add_version("2-zen3", ("x86_64/amd/zen3", []))

        partition = self.partition("x86_64/amd/zen4")
        self.assertEqual(self.found(partition), ["1-zen4", "2-zen3"])

    def test_partition_without_build_offers_nothing(self):
        self.add_version("1-generic", ("riscv64/generic", []))

        partition = self.partition("riscv64/generic")
        self.assertEqual(self.found(partition), [])
        self.assertEqual(software.offered_setup_lines(self.offering, partition), set())

    def test_gpu_partition_skips_builds_for_other_gpus(self):
        self.add_version("1-cpu", ("x86_64/amd/zen4", []))
        self.add_version("2-cc80", ("x86_64/amd/zen4", ["nvidia/cc80"]))
        self.add_version("3-cc90", ("x86_64/amd/zen4", ["nvidia/cc80", "nvidia/cc90"]))

        partition = self.partition("x86_64/amd/zen4", gpu_arch="nvidia/cc90")
        self.assertEqual(self.found(partition), ["1-cpu", "3-cc90"])

    def test_spack_offered_on_partition_with_cpu(self):
        spack = factories.SoftwareCatalogFactory(
            name="Spack", version="2026.01.01", catalog_type="source_package"
        )
        factories.OfferingSoftwareCatalogFactory(offering=self.offering, catalog=spack)
        lammps = factories.SoftwarePackageFactory(catalog=spack, name="lammps")
        factories.SoftwareVersionFactory(package=lammps, version="20240829")

        matches = software.find_software(
            self.offering, query="lammps", partition=self.partition("x86_64/amd/zen4")
        )
        self.assertEqual([m.load for m in matches], ["spack load lammps@20240829"])
        self.assertEqual(matches[0].setup, [])

    def test_hand_made_binary_catalog_is_not_eessi(self):
        site = factories.SoftwareCatalogFactory(
            name="Site builds", version="1.0", catalog_type="binary_runtime"
        )
        factories.OfferingSoftwareCatalogFactory(
            offering=self.offering,
            catalog=site,
            enabled_cpu_family=[],
            enabled_cpu_microarchitectures=[],
        )
        tool = factories.SoftwarePackageFactory(catalog=site, name="sitetool")
        factories.SoftwareVersionFactory(package=tool, version="2.0")

        (match,) = software.find_software(
            self.offering, query="sitetool", partition=self.partition("x86_64/amd/zen4")
        )
        self.assertEqual(match.setup, [])
        self.assertEqual(match.load, "module load sitetool/2.0")

    def test_hand_made_catalog_builds_for_other_cpus_are_not_offered(self):
        site = factories.SoftwareCatalogFactory(
            name="Site builds", version="1.0", catalog_type="binary_runtime"
        )
        factories.OfferingSoftwareCatalogFactory(offering=self.offering, catalog=site)
        tool = factories.SoftwarePackageFactory(catalog=site, name="sitetool")
        self.add_version("1-arm", ("aarch64/generic", []), package=tool)
        self.add_version("2-x86", ("x86_64/generic", []), package=tool)

        partition = self.partition("x86_64/amd/zen4")
        self.assertEqual(self.found(partition, query="sitetool"), ["2-x86"])

    def test_renamed_eessi_catalog_keeps_its_setup_line(self):
        self.eessi.name = "EESSI prod"
        self.eessi.save()
        self.add_version("1-zen4", ("x86_64/amd/zen4", []))

        (match,) = software.find_software(self.offering, query="gromacs")
        self.assertEqual(match.setup, ["module load EESSI/2023.06"])


class ModuleWarningsTest(SoftwareTestBase):
    def setUp(self):
        super().setUp()
        self.add_version("2024.1", ("x86_64/amd/zen4", []))
        self.add_version("2023.3", ("x86_64/amd/zen3", []))
        self.partition("x86_64/amd/zen4")

    def validate(self, modules, partition="batch"):
        spec = scripts.JobSpec(
            name="job", commands="true", partition=partition, modules=modules
        )
        return scripts.validate(spec, self.offering)

    def test_offered_modules_pass(self):
        result = self.validate(
            ["module load EESSI/2023.06", "module load GROMACS/2024.1"]
        )
        self.assertEqual(result.warnings, [])
        self.assertTrue(result.is_valid)

    def test_module_not_built_for_partition_warns(self):
        result = self.validate(
            ["module purge", "module load EESSI/2023.06 GROMACS/2023.3"]
        )
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("'module load GROMACS/2023.3'", result.warnings[0])
        self.assertIn("partition 'batch'", result.warnings[0])
        self.assertTrue(result.is_valid)

    def test_short_and_bare_module_names_pass(self):
        self.add_version("2025.2-foss-2025a", ("x86_64/amd/zen4", []))
        result = self.validate(
            [
                "module load EESSI/2023.06",
                "module load gromacs/2025.2",
                "module load GROMACS",
                "module load -q GROMACS/2025.2-foss-2025a",
            ]
        )
        self.assertEqual(result.warnings, [])

    def test_modules_unknown_to_the_catalogs_are_not_checked(self):
        result = self.validate(["module load cray-mpich/8.1", "module load GROMCS/1"])
        self.assertEqual(result.warnings, [])

    def test_eessi_version_not_linked_warns(self):
        result = self.validate(["module load EESSI/2025.06"])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("EESSI/2025.06", result.warnings[0])

    def test_spack_spec_without_version_passes(self):
        spack = factories.SoftwareCatalogFactory(
            name="Spack", version="2026.01.01", catalog_type="source_package"
        )
        factories.OfferingSoftwareCatalogFactory(offering=self.offering, catalog=spack)
        lammps = factories.SoftwarePackageFactory(catalog=spack, name="lammps")
        factories.SoftwareVersionFactory(package=lammps, version="20240829")

        self.assertEqual(self.validate(["spack load lammps"]).warnings, [])
        self.assertEqual(self.validate(["spack load lammps@20240829"]).warnings, [])
        self.assertEqual(len(self.validate(["spack load lammps@1"]).warnings), 1)

    def test_two_eessi_stacks_warn(self):
        result = self.validate(
            ["module load EESSI/2023.06", "module load EESSI/2025.06"]
        )
        self.assertTrue(
            any("several EESSI stacks" in warning for warning in result.warnings)
        )
        self.assertTrue(result.is_valid)

    def test_offering_without_catalogs_is_not_checked(self):
        self.link.delete()
        result = self.validate(["module load anything/1.0"])
        self.assertEqual(result.warnings, [])
